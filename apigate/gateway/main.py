"""
gateway/main.py

Wires the FastAPI app together: middleware stack, routes, and
startup/shutdown lifecycle for shared resources (Redis pool, HTTP
client).

Middleware ordering matters — Starlette applies middleware in
REVERSE order of .add_middleware() calls (last added = runs first).
We need, in actual execution order per request:

    1. LoggingMiddleware       (logs every request, even rejected ones)
    2. JWTAuthMiddleware       (populates request.state.user)
    3. SlidingWindowRateLimiter (keys by request.state.user if present —
                                  must run AFTER auth, see rate_limiter.py)

So we add them to FastAPI in the OPPOSITE order: rate limiter first,
then auth, then logging — so logging ends up outermost/first.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from prometheus_fastapi_instrumentator import Instrumentator

from gateway.auth.jwt_handler import create_access_token
from gateway.auth.schemas import TokenRequest, TokenResponse
from gateway.config import settings
from gateway.middleware.auth_middleware import JWTAuthMiddleware
from gateway.middleware.logging_middleware import LoggingMiddleware
from gateway.middleware.rate_limiter import SlidingWindowRateLimiter
from gateway.redis_client.client import close_redis_client, get_redis_client, ping
from gateway.routing.proxy import close_http_client, proxy_request
from gateway.routing.service_registry import list_registered_services

logging.basicConfig(level=settings.LOG_LEVEL.upper())
logger = logging.getLogger("apigate.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup — establish shared connections eagerly so the first
    # real request isn't slowed by lazy-init, and so /health can
    # report Redis status immediately.
    await get_redis_client()
    logger.info("%s starting up (env=%s)", settings.APP_NAME, settings.ENV)
    yield
    # Shutdown — release pooled connections cleanly.
    await close_redis_client()
    await close_http_client()
    logger.info("%s shut down", settings.APP_NAME)


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)

# See module docstring for why this order = correct execution order.
app.add_middleware(SlidingWindowRateLimiter)
app.add_middleware(JWTAuthMiddleware)
app.add_middleware(LoggingMiddleware)

# Prometheus /metrics endpoint — auto-instruments request count/latency
# and is already in settings.AUTH_EXEMPT_PATHS / rate-limiter-exempt.
Instrumentator().instrument(app).expose(app, endpoint="/metrics")


@app.get("/health")
async def health():
    redis_ok = await ping()
    return {
        "status": "ok" if redis_ok else "degraded",
        "redis": "up" if redis_ok else "down",
        "services": list_registered_services(),
    }


@app.post("/auth/token", response_model=TokenResponse)
async def login(body: TokenRequest):
    """
    Minimal demo login — issues a JWT for any non-empty username/password.
    Replace with real credential verification before production use.
    """
    token = create_access_token(subject=body.username)
    return TokenResponse(
        access_token=token,
        expires_in_minutes=settings.JWT_EXPIRE_MINUTES,
    )


@app.api_route(
    "/{full_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
async def gateway_catch_all(request: Request, full_path: str):
    """
    Catch-all reverse-proxy route. Anything not matched by an explicit
    route above (/health, /auth/token, /metrics, /docs) falls through
    here and gets forwarded to the resolved backend service.
    """
    return await proxy_request(request, request.url.path)
