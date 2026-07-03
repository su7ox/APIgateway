"""
gateway/metrics/prometheus_metrics.py

Custom Prometheus metrics for gateway-specific behavior that the
generic prometheus_fastapi_instrumentator (wired up in main.py) can't
see: circuit breaker state transitions, rate-limit rejections, and
per-backend proxy outcomes.

These are plain prometheus_client collectors, registered on import via
the default registry — the same registry the instrumentator's
Instrumentator().expose(app) call serves at /metrics, so everything
shows up on one scrape endpoint.

Call sites (added to keep this module import-only-cost, i.e. no
circular imports with proxy.py / circuit_breaker.py / rate_limiter.py):
  - gateway/routing/proxy.py       -> record_proxy_request(), record_circuit_state()
  - gateway/middleware/rate_limiter.py -> record_rate_limit_rejection()
"""

from prometheus_client import Counter, Gauge, Histogram

# ----------------------------------------------------------------------
# Reverse-proxy request outcomes, labeled by backend service and result.
# Mirrors the JSON error shapes returned by proxy.py (bad_gateway,
# upstream_timeout, circuit_open) plus "success" for 2xx/normal relays.
# ----------------------------------------------------------------------
PROXY_REQUESTS_TOTAL = Counter(
    "apigate_proxy_requests_total",
    "Total requests proxied to backend services, labeled by outcome",
    labelnames=["service_name", "outcome"],
)

PROXY_REQUEST_DURATION_SECONDS = Histogram(
    "apigate_proxy_request_duration_seconds",
    "Time spent forwarding a request to a backend and receiving a response",
    labelnames=["service_name"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# ----------------------------------------------------------------------
# Circuit breaker state, labeled by service. Gauge value is the
# CircuitState enum encoded as an int so Grafana can graph transitions
# over time (0=closed, 1=half_open, 2=open).
# ----------------------------------------------------------------------
CIRCUIT_BREAKER_STATE = Gauge(
    "apigate_circuit_breaker_state",
    "Current circuit breaker state per backend service "
    "(0=closed, 1=half_open, 2=open)",
    labelnames=["service_name"],
)

CIRCUIT_BREAKER_TRANSITIONS_TOTAL = Counter(
    "apigate_circuit_breaker_transitions_total",
    "Total circuit breaker state transitions, labeled by service and target state",
    labelnames=["service_name", "to_state"],
)

# ----------------------------------------------------------------------
# Rate limiting
# ----------------------------------------------------------------------
RATE_LIMIT_REJECTIONS_TOTAL = Counter(
    "apigate_rate_limit_rejections_total",
    "Total requests rejected due to rate limiting, labeled by client key type",
    labelnames=[
        "client_key_type"
    ],  # "user" or "ip" — see rate_limiter._resolve_client_key
)

# ----------------------------------------------------------------------
# Auth
# ----------------------------------------------------------------------
AUTH_FAILURES_TOTAL = Counter(
    "apigate_auth_failures_total",
    "Total requests rejected by JWTAuthMiddleware, labeled by reason",
    labelnames=["reason"],
)


_STATE_TO_INT = {"closed": 0, "half_open": 1, "open": 2}


def record_proxy_request(
    service_name: str, outcome: str, duration_seconds: float
) -> None:
    """
    Call once per proxied request from proxy.py, after the backend
    call resolves (success, bad_gateway, upstream_timeout) or is
    rejected (circuit_open, unknown_service).
    """
    PROXY_REQUESTS_TOTAL.labels(service_name=service_name, outcome=outcome).inc()
    if outcome not in ("circuit_open", "unknown_service"):
        # Only record latency for calls that actually reached the network —
        # fail-fast rejections would skew the histogram toward 0s.
        PROXY_REQUEST_DURATION_SECONDS.labels(service_name=service_name).observe(
            duration_seconds
        )


def record_circuit_state(service_name: str, state: str) -> None:
    """
    Call from circuit_breaker.py whenever state is read or transitions
    (before_call, record_success, record_failure) so the gauge always
    reflects the latest known state.
    """
    if state in _STATE_TO_INT:
        CIRCUIT_BREAKER_STATE.labels(service_name=service_name).set(
            _STATE_TO_INT[state]
        )


def record_circuit_transition(service_name: str, to_state: str) -> None:
    """Call only on an actual transition (not every state read) to track trip/recovery events."""
    CIRCUIT_BREAKER_TRANSITIONS_TOTAL.labels(
        service_name=service_name, to_state=to_state
    ).inc()
    record_circuit_state(service_name, to_state)


def record_rate_limit_rejection(client_key: str) -> None:
    """
    Call from rate_limiter.py's dispatch() on a 429. client_key is the
    full "user:xxx" or "ip:xxx" string from _resolve_client_key — we
    only want the type as a label (not the raw identity, to avoid
    unbounded cardinality).
    """
    key_type = client_key.split(":", 1)[0] if ":" in client_key else "unknown"
    RATE_LIMIT_REJECTIONS_TOTAL.labels(client_key_type=key_type).inc()


def record_auth_failure(reason: str) -> None:
    """Call from auth_middleware.py's dispatch() on a 401."""
    AUTH_FAILURES_TOTAL.labels(reason=reason).inc()
