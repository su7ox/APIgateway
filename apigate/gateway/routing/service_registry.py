"""
gateway/routing/service_registry.py

Resolves an incoming gateway path (e.g. /service-a/foo/bar) to:
  - the logical service name ("service-a") — used as the key for
    circuit breaker state, rate-limit exemptions, and metrics labels
  - the backend base URL (from settings.SERVICE_REGISTRY)
  - the remaining downstream path to forward ("/foo/bar")

Kept isolated from proxy.py so it's independently testable and so
any future routing logic (weighted routing, canary releases, etc.)
can reuse resolution without pulling in the actual HTTP forwarding.
"""

import logging
from dataclasses import dataclass

from gateway.config import settings

logger = logging.getLogger("apigate.service_registry")


class UnknownServiceError(Exception):
    """Raised when a request's path doesn't match any registered service prefix."""

    def __init__(self, prefix: str):
        self.prefix = prefix
        super().__init__(f"No backend service registered for prefix '{prefix}'")


@dataclass(frozen=True)
class ResolvedRoute:
    """Result of resolving an incoming gateway path to a backend service."""

    service_name: str  # e.g. "service-a" — used as CB/rate-limit/metrics key
    base_url: str  # e.g. "http://service_a:8001"
    downstream_path: str  # e.g. "/foo/bar" — path to forward to the backend


def resolve_route(path: str) -> ResolvedRoute:
    """
    Resolve an incoming gateway request path to a backend service.

    Expects paths of the form /<service-prefix>/<rest...>, where the
    prefix matches a key in settings.SERVICE_REGISTRY (e.g. "service-a").
    Raises UnknownServiceError if the prefix doesn't match a registered
    service, so the proxy layer can return a clean 404 instead of an
    unhandled KeyError.

    Examples:
        /service-a/orders/123  -> ResolvedRoute("service-a", "http://service_a:8001", "/orders/123")
        /service-a             -> ResolvedRoute("service-a", "http://service_a:8001", "/")
    """
    trimmed = path.strip("/")
    if not trimmed:
        raise UnknownServiceError(prefix="")

    parts = trimmed.split("/", 1)
    prefix = parts[0]
    remainder = parts[1] if len(parts) > 1 else ""

    base_url = settings.SERVICE_REGISTRY.get(prefix)
    if base_url is None:
        raise UnknownServiceError(prefix=prefix)

    downstream_path = f"/{remainder}" if remainder else "/"

    return ResolvedRoute(
        service_name=prefix,
        base_url=base_url,
        downstream_path=downstream_path,
    )


def list_registered_services() -> dict[str, str]:
    """Return the full service registry — used by an admin/health endpoint."""
    return dict(settings.SERVICE_REGISTRY)
