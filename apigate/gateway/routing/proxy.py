"""
gateway/routing/proxy.py

Reverse-proxy layer: takes an incoming Starlette Request, resolves it
to a backend service via service_registry, and forwards it through
httpx — wrapped by the circuit breaker so a struggling backend gets
isolated instead of hammered.

Call chain per request:
    resolve_route()                -> which backend + downstream path
    circuit_breaker.before_call()  -> raises CircuitOpenError if OPEN
    httpx forward                  -> actual HTTP call to the backend
    circuit_breaker.record_success() / record_failure()
    build Response back to the original caller
"""

import logging

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from gateway.config import settings
from gateway.middleware.circuit_breaker import CircuitBreaker, CircuitOpenError
from gateway.routing.service_registry import UnknownServiceError, resolve_route

logger = logging.getLogger("apigate.proxy")

# Hop-by-hop headers (RFC 7230 §6.1) plus 'host'/'content-length' must
# not be blindly forwarded in either direction — they're connection-
# specific or get recalculated by httpx from the actual body.
_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

# Module-level singleton httpx client, mirroring the shared Redis pool
# pattern in redis_client/client.py — reused across requests so we get
# connection pooling/keep-alive to backends instead of a new TCP
# handshake per proxied call.
_http_client: httpx.AsyncClient | None = None

# One breaker instance is fine — state actually lives in Redis, keyed
# per service_name, so this object itself is stateless/shareable.
_breaker = CircuitBreaker()


def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=settings.PROXY_TIMEOUT_SECONDS)
        logger.info("HTTP client for backend proxying created")
    return _http_client


async def close_http_client() -> None:
    """Called from main.py's lifespan shutdown hook, alongside close_redis_client()."""
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None
        logger.info("HTTP client for backend proxying closed")


def _filter_headers(headers) -> dict:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP_HEADERS}


async def proxy_request(request: Request, gateway_path: str) -> Response:
    """
    Forward `request` to the appropriate backend service, resolved
    from `gateway_path` (pass the full incoming path, e.g.
    "/service-a/orders/123" — resolve_route() handles prefix parsing).
    """
    try:
        route = resolve_route(gateway_path)
    except UnknownServiceError as exc:
        logger.info("Unknown service prefix requested: %r", exc.prefix)
        return JSONResponse(
            status_code=404,
            content={"error": "unknown_service", "detail": str(exc)},
        )

    try:
        await _breaker.before_call(route.service_name)
    except CircuitOpenError as exc:
        logger.warning("Circuit OPEN for '%s' — failing fast", route.service_name)
        return JSONResponse(
            status_code=503,
            content={"error": "circuit_open", "detail": str(exc)},
            headers={"Retry-After": f"{exc.retry_after_seconds:.0f}"},
        )

    client = get_http_client()
    target_url = f"{route.base_url}{route.downstream_path}"

    body = await request.body()
    forward_headers = _filter_headers(request.headers)

    try:
        backend_response = await client.request(
            method=request.method,
            url=target_url,
            params=request.query_params,
            headers=forward_headers,
            content=body,
        )
    except httpx.TimeoutException:
        await _breaker.record_failure(route.service_name)
        logger.warning("Timeout proxying to '%s' (%s)", route.service_name, target_url)
        return JSONResponse(
            status_code=504,
            content={
                "error": "upstream_timeout",
                "detail": f"'{route.service_name}' did not respond in time",
            },
        )
    except httpx.RequestError as exc:
        await _breaker.record_failure(route.service_name)
        logger.warning("Error proxying to '%s': %s", route.service_name, exc)
        return JSONResponse(
            status_code=502,
            content={
                "error": "bad_gateway",
                "detail": f"Could not reach '{route.service_name}'",
            },
        )

    # A backend 5xx counts as a circuit-breaker failure (the backend is
    # unhealthy) but we still relay the actual response to the caller —
    # we're not swallowing legitimate error payloads/status codes.
    if backend_response.status_code >= 500:
        await _breaker.record_failure(route.service_name)
    else:
        await _breaker.record_success(route.service_name)

    return Response(
        content=backend_response.content,
        status_code=backend_response.status_code,
        headers=_filter_headers(backend_response.headers),
        media_type=backend_response.headers.get("content-type"),
    )