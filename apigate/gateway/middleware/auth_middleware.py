"""
gateway/middleware/auth_middleware.py

Starlette/FastAPI middleware that enforces JWT authentication on every
incoming request, except for paths listed in settings.AUTH_EXEMPT_PATHS
(health checks, metrics scraping, docs, and the login endpoint itself).

On success, the decoded token payload is attached to `request.state.user`
so downstream handlers (and the reverse-proxy logic) can access the
caller's identity without re-parsing the token.
"""

import logging

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from gateway.auth.jwt_handler import (
    TokenError,
    decode_access_token,
    extract_bearer_token,
)
from gateway.config import settings

logger = logging.getLogger("apigate.auth_middleware")


class JWTAuthMiddleware(BaseHTTPMiddleware):
    """
    Validates the `Authorization: Bearer <token>` header on every
    request whose path is not in AUTH_EXEMPT_PATHS.

    Rejects with 401 JSON responses on any failure — missing header,
    malformed header, invalid signature, or expired token — so callers
    get a consistent, machine-readable error shape rather than a
    stack trace or generic 500.
    """

    def __init__(self, app: ASGIApp):
        super().__init__(app)
        # Normalize exempt paths once for fast lookup per-request.
        self._exempt_paths = set(settings.AUTH_EXEMPT_PATHS)

    def _is_exempt(self, path: str) -> bool:
        # Exact match OR prefix match (so /docs/oauth2-redirect etc. also
        # pass through without a token).
        if path in self._exempt_paths:
            return True
        return any(path.startswith(p) for p in self._exempt_paths if p != "/")

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint):
        if self._is_exempt(request.url.path):
            return await call_next(request)

        auth_header = request.headers.get("Authorization")

        try:
            token = extract_bearer_token(auth_header)
            payload = decode_access_token(token)
        except TokenError as exc:
            logger.info(
                "Rejected unauthenticated request to %s: %s",
                request.url.path,
                exc.detail,
            )
            return JSONResponse(
                status_code=401,
                content={"error": "unauthorized", "detail": exc.detail},
                headers={"WWW-Authenticate": "Bearer"},
            )

        request.state.user = payload

        return await call_next(request)
