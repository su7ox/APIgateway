"""
gateway/redis_client/client.py

Provides a single, shared async Redis connection pool for the whole
gateway process. Both the rate limiter (sliding window counters) and
the circuit breaker (state per backend service) read/write through
this client, so their state is consistent across all gateway workers
and survives individual request lifecycles.

Usage:
    from gateway.redis_client.client import get_redis_client

    redis = await get_redis_client()
    await redis.set("foo", "bar")
"""

import logging

import redis.asyncio as redis

from gateway.config import settings

logger = logging.getLogger("apigate.redis")

# Module-level singleton pool. Created lazily on first use and reused
# for the lifetime of the process (see gateway/main.py lifespan hooks
# for explicit startup/shutdown wiring).
_redis_pool: redis.ConnectionPool | None = None
_redis_client: redis.Redis | None = None


def _build_pool() -> redis.ConnectionPool:
    """Create a new Redis connection pool from settings.REDIS_URL."""
    return redis.ConnectionPool.from_url(
        settings.REDIS_URL,
        decode_responses=True,   # always get back str, not bytes
        max_connections=50,
    )


async def get_redis_client() -> redis.Redis:
    """
    Return the shared Redis client, creating the connection pool on
    first call. Safe to call repeatedly/concurrently — the pool is
    created once and reused.
    """
    global _redis_pool, _redis_client

    if _redis_client is None:
        _redis_pool = _build_pool()
        _redis_client = redis.Redis(connection_pool=_redis_pool)
        logger.info("Redis connection pool created (%s)", settings.REDIS_URL)

    return _redis_client


async def close_redis_client() -> None:
    """
    Gracefully close the Redis client and connection pool.
    Called from the FastAPI app's shutdown/lifespan event so
    connections don't leak when the gateway stops.
    """
    global _redis_pool, _redis_client

    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None
        logger.info("Redis client closed")

    if _redis_pool is not None:
        await _redis_pool.disconnect()
        _redis_pool = None
        logger.info("Redis connection pool disconnected")


async def ping() -> bool:
    """
    Health-check helper — used by the /health endpoint to confirm
    the gateway can actually reach Redis (not just that the process
    is up).
    """
    try:
        client = await get_redis_client()
        return await client.ping()
    except Exception:
        logger.exception("Redis ping failed")
        return False