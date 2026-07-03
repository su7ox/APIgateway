"""
gateway/middleware/circuit_breaker.py

Circuit breaker pattern implementation, per downstream backend service.

States:
    CLOSED     - normal operation. Requests pass through to the backend.
                 Consecutive failures are counted; hitting the threshold
                 trips the breaker to OPEN.
    OPEN       - backend is considered unhealthy. Requests are rejected
                 immediately (fail fast) WITHOUT calling the backend,
                 for CB_RECOVERY_TIMEOUT_SECONDS. This is what prevents
                 cascade failures: a struggling backend isn't hammered
                 with more traffic while it's down.
    HALF_OPEN  - after the recovery timeout elapses, a small number of
                 trial requests (CB_HALF_OPEN_MAX_CALLS) are allowed
                 through to test if the backend has recovered.
                    - any failure during HALF_OPEN -> back to OPEN
                    - enough successes -> CLOSED (fully recovered)

State is stored in a Redis hash per service (key: "cb:{service_name}")
so all gateway workers/replicas share the same view of each backend's
health, not just the process handling the current request.
"""

import logging
import time
from enum import Enum

from gateway.config import settings
from gateway.redis_client.client import get_redis_client

logger = logging.getLogger("apigate.circuit_breaker")

REDIS_KEY_PREFIX = "cb"


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised when a call is rejected because the circuit is OPEN."""
    def __init__(self, service_name: str, retry_after_seconds: float):
        self.service_name = service_name
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            f"Circuit breaker OPEN for service '{service_name}'; "
            f"retry after {retry_after_seconds:.1f}s"
        )


class CircuitBreaker:
    """
    Redis-backed circuit breaker keyed by backend service name.

    Usage (from the proxy layer):

        breaker = CircuitBreaker()
        await breaker.before_call("service-a")   # raises CircuitOpenError if tripped
        try:
            result = await call_backend(...)
        except Exception:
            await breaker.record_failure("service-a")
            raise
        else:
            await breaker.record_success("service-a")
    """

    def __init__(self):
        self.failure_threshold = settings.CB_FAILURE_THRESHOLD
        self.recovery_timeout = settings.CB_RECOVERY_TIMEOUT_SECONDS
        self.half_open_max_calls = settings.CB_HALF_OPEN_MAX_CALLS

    def _key(self, service_name: str) -> str:
        return f"{REDIS_KEY_PREFIX}:{service_name}"

    async def _get_state(self, service_name: str) -> dict:
        redis = await get_redis_client()
        raw = await redis.hgetall(self._key(service_name))

        if not raw:
            # No state yet -> service has never failed -> CLOSED.
            return {
                "state": CircuitState.CLOSED.value,
                "failure_count": "0",
                "opened_at": "0",
                "half_open_calls": "0",
            }
        return raw

    async def _set_state(self, service_name: str, **fields) -> None:
        redis = await get_redis_client()
        await redis.hset(self._key(service_name), mapping=fields)

    async def get_current_state(self, service_name: str) -> CircuitState:
        """Public accessor — used by the /health or admin endpoints."""
        raw = await self._get_state(service_name)
        return CircuitState(raw["state"])

    async def before_call(self, service_name: str) -> None:
        """
        Call this BEFORE forwarding a request to the backend.
        Raises CircuitOpenError if the call should be blocked.
        Transitions OPEN -> HALF_OPEN automatically once the recovery
        timeout has elapsed.
        """
        raw = await self._get_state(service_name)
        state = CircuitState(raw["state"])
        now = time.time()

        if state == CircuitState.CLOSED:
            return  # normal operation, allow the call

        if state == CircuitState.OPEN:
            opened_at = float(raw["opened_at"])
            elapsed = now - opened_at

            if elapsed >= self.recovery_timeout:
                # Recovery window has passed -> allow a trial call by
                # transitioning to HALF_OPEN.
                logger.info(
                    "Circuit for '%s' moving OPEN -> HALF_OPEN after %.1fs",
                    service_name, elapsed,
                )
                await self._set_state(
                    service_name,
                    state=CircuitState.HALF_OPEN.value,
                    half_open_calls="1",  # this call counts as the first trial
                )
                return

            # Still within the timeout window -> fail fast.
            raise CircuitOpenError(
                service_name, retry_after_seconds=self.recovery_timeout - elapsed
            )

        if state == CircuitState.HALF_OPEN:
            half_open_calls = int(raw["half_open_calls"])
            if half_open_calls >= self.half_open_max_calls:
                # Already used up our trial calls for this half-open
                # window without a definitive result yet -> fail fast
                # rather than piling more load on a possibly-still-sick
                # backend.
                raise CircuitOpenError(service_name, retry_after_seconds=1.0)

            await self._set_state(
                service_name, half_open_calls=str(half_open_calls + 1)
            )
            return

    async def record_success(self, service_name: str) -> None:
        """Call after a successful backend response."""
        raw = await self._get_state(service_name)
        state = CircuitState(raw["state"])

        if state == CircuitState.HALF_OPEN:
            # A trial call succeeded -> backend looks healthy again.
            # Fully reset the breaker to CLOSED.
            logger.info("Circuit for '%s' recovering HALF_OPEN -> CLOSED", service_name)
            await self._set_state(
                service_name,
                state=CircuitState.CLOSED.value,
                failure_count="0",
                opened_at="0",
                half_open_calls="0",
            )
        elif state == CircuitState.CLOSED:
            # Reset any partial failure streak on a healthy response.
            if raw["failure_count"] != "0":
                await self._set_state(service_name, failure_count="0")
        # If somehow CLOSED->success while OPEN raced in, next
        # before_call() call will resolve state correctly regardless.

    async def record_failure(self, service_name: str) -> None:
        """Call after a failed backend response (error/timeout/5xx)."""
        raw = await self._get_state(service_name)
        state = CircuitState(raw["state"])
        now = time.time()

        if state == CircuitState.HALF_OPEN:
            # Trial call failed -> backend still unhealthy. Reopen fully
            # and restart the recovery timeout.
            logger.warning(
                "Circuit for '%s' failed trial call, HALF_OPEN -> OPEN", service_name
            )
            await self._set_state(
                service_name,
                state=CircuitState.OPEN.value,
                opened_at=str(now),
                half_open_calls="0",
            )
            return

        if state == CircuitState.CLOSED:
            failure_count = int(raw["failure_count"]) + 1

            if failure_count >= self.failure_threshold:
                logger.warning(
                    "Circuit for '%s' tripped CLOSED -> OPEN after %d "
                    "consecutive failures",
                    service_name, failure_count,
                )
                await self._set_state(
                    service_name,
                    state=CircuitState.OPEN.value,
                    failure_count="0",
                    opened_at=str(now),
                )
            else:
                await self._set_state(service_name, failure_count=str(failure_count))
        # If already OPEN, nothing to do — before_call() already fails
        # fast so record_failure shouldn't normally be reached here.