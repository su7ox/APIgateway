"""
gateway/middleware/rate_limiter.py

Redis-backed sliding-window rate limiter.

Why a sliding window (vs. a simple fixed-window counter):
A fixed window (e.g. "max 100 requests per clock-minute") allows a
client to send 100 requests at 0:59 and another 100 at 1:00 — 200
requests in ~1 second. A sliding window tracks the exact timestamps
of recent requests in a Redis sorted set (ZSET) and only counts the
ones that fall within the last N seconds *relative to now*, so bursts
across window boundaries are correctly capped.

Algorithm per request, for a given client key:
  1. ZREMRANGEBYSCORE  -> drop entries older than (now - window)
  2. ZCARD             -> count remaining entries (requests in window)
  3. if count >= limit: reject with 429
  4. else: ZADD now -> record this request, EXPIRE key, allow it

Steps are pipelined into a single round-trip to Redis for performance.
"""

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from gateway.config import settings
from gateway.redis_client.client import get_redis_client

logger = logging.getLogger("apigate.rate_limiter")

REDIS_KEY_PREFIX = "ratelimit"


def _resolve_client_key(request: Request) -> str:
    """
    Determine the identity to rate-limit by.

    Prefer the authenticated user's subject (set by JWTAuthMiddleware)
    so limits are per-user even behind a shared IP/NAT. Fall back to
    client IP for exempt/unauthenticated paths (e.g. /auth/token,
    which we still want to protect against brute-forcing).
    """
    user = getattr(request.state, "user", None)
    if user is not None:
        return f"user:{user.sub}"

    client_host = request.client.host if request.client else "unknown"
    return f"ip:{client_host}"


class SlidingWindowRateLimiter(BaseHTTPMiddleware):
    """
    Enforces settings.RATE_LIMIT_MAX_REQUESTS per
    settings.RATE_LIMIT_WINDOW_SECONDS, per client key, using a Redis
    sorted set as the sliding window store.

    NOTE: This middleware must run AFTER JWTAuthMiddleware in the
    middleware stack (added later in main.py, i.e. closer to the
    route) so request.state.user is already populated when we key by
    user identity. See main.py for the exact ordering.
    """

    def __init__(self, app: ASGIApp):
        super().__init__(app)
        self.window_seconds = settings.RATE_LIMIT_WINDOW_SECONDS
        self.max_requests = settings.RATE_LIMIT_MAX_REQUESTS

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ):
        # Never rate-limit the metrics endpoint — Prometheus scrapes it
        # frequently and it's not attacker-facing.
        if request.url.path == "/metrics":
            return await call_next(request)

        client_key = _resolve_client_key(request)
        redis_key = f"{REDIS_KEY_PREFIX}:{client_key}"

        now = time.time()
        window_start = now - self.window_seconds

        redis = await get_redis_client()

        # Unique member per request so simultaneous requests at the
        # same millisecond don't collide/overwrite each other in the ZSET.
        member = f"{now}:{uuid.uuid4().hex}"

        pipe = redis.pipeline()
        pipe.zremrangebyscore(redis_key, 0, window_start)   # evict old entries
        pipe.zcard(redis_key)                               # count current entries
        pipe.zadd(redis_key, {member: now})                 # record this request
        pipe.expire(redis_key, self.window_seconds)         # auto-cleanup if idle
        _, current_count, _, _ = await pipe.execute()

        if current_count >= self.max_requests:
            # We already added `member` above; remove it since this
            # request is being rejected and shouldn't count against
            # the client's next attempt.
            await redis.zrem(redis_key, member)

            logger.info(
                "Rate limit exceeded for %s (%d/%d in %ds window)",
                client_key, current_count, self.max_requests, self.window_seconds,
            )
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limited",
                    "detail": f"Rate limit exceeded: {self.max_requests} "
                              f"requests per {self.window_seconds}s",
                },
                headers={"Retry-After": str(self.window_seconds)},
            )

        response = await call_next(request)

        # Expose remaining quota to the caller for well-behaved clients.
        remaining = max(self.max_requests - current_count - 1, 0)
        response.headers["X-RateLimit-Limit"] = str(self.max_requests)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Window"] = str(self.window_seconds)

        return response