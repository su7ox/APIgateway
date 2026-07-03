"""
gateway/middleware/logging_middleware.py

Request/response logging middleware. Runs OUTERMOST in the middleware
stack (added last in main.py, per its ordering docstring) so it logs
every request that hits the gateway — including ones later rejected
by auth or rate limiting — and captures the final status code/latency
regardless of which layer produced the response.
"""

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.types import ASGIApp

logger = logging.getLogger("apigate.access")


class LoggingMiddleware(BaseHTTPMiddleware):
    """
    Logs one line per request: a generated request id, method, path,
    resulting status code, and duration in milliseconds. The request
    id is also stamped onto request.state and echoed back as a
    response header (X-Request-ID) so it can be correlated with
    backend-side logs when troubleshooting a specific call.
    """

    def __init__(self, app: ASGIApp):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint):
        request_id = uuid.uuid4().hex[:12]
        request.state.request_id = request_id

        start = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            duration_ms = (time.perf_counter() - start) * 1000
            logger.exception(
                "request_id=%s method=%s path=%s status=500 duration_ms=%.1f (unhandled exception)",
                request_id, request.method, request.url.path, duration_ms,
            )
            raise

        duration_ms = (time.perf_counter() - start) * 1000

        # Log level scales with status: 5xx are errors worth surfacing,
        # 4xx are client-caused but worth a warning, 2xx/3xx are routine.
        if response.status_code >= 500:
            log_fn = logger.error
        elif response.status_code >= 400:
            log_fn = logger.warning
        else:
            log_fn = logger.info

        log_fn(
            "request_id=%s method=%s path=%s status=%d duration_ms=%.1f",
            request_id, request.method, request.url.path,
            response.status_code, duration_ms,
        )

        response.headers["X-Request-ID"] = request_id
        return response