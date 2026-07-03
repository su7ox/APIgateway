"""
gateway/config.py

Central configuration for APIGate.

All other modules (redis_client, auth, middleware, routing, metrics) import
`settings` from here instead of reading os.environ directly. This keeps
configuration in one place and makes it trivial to override via a .env file
or environment variables when running in Docker.
"""

from functools import lru_cache
from typing import Dict

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BackendService(BaseSettings):
    """
    Represents a single downstream microservice that the gateway can
    route requests to.
    """
    name: str
    base_url: str


class Settings(BaseSettings):
    """
    Application-wide settings, populated from environment variables
    (or a .env file). See .env.example for the full list of overridable keys.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # General app settings
    # ------------------------------------------------------------------
    APP_NAME: str = "APIGate"
    ENV: str = Field(default="development")  # development | production
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    LOG_LEVEL: str = "info"

    # ------------------------------------------------------------------
    # Redis (used by rate limiter + circuit breaker for shared state)
    # ------------------------------------------------------------------
    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: str | None = None

    @property
    def REDIS_URL(self) -> str:
        auth = f":{self.REDIS_PASSWORD}@" if self.REDIS_PASSWORD else ""
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    # ------------------------------------------------------------------
    # JWT Auth
    # ------------------------------------------------------------------
    JWT_SECRET_KEY: str = "CHANGE_ME_IN_PRODUCTION"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 60

    # Routes that do NOT require a valid JWT (health checks, metrics, login)
    AUTH_EXEMPT_PATHS: list[str] = [
        "/health",
        "/metrics",
        "/docs",
        "/openapi.json",
        "/auth/token",
    ]

    # ------------------------------------------------------------------
    # Rate limiting (Redis-backed sliding window)
    # ------------------------------------------------------------------
    RATE_LIMIT_WINDOW_SECONDS: int = 60
    RATE_LIMIT_MAX_REQUESTS: int = 100  # max requests per window per client

    # ------------------------------------------------------------------
    # Circuit breaker
    # ------------------------------------------------------------------
    CB_FAILURE_THRESHOLD: int = 5          # consecutive failures -> OPEN
    CB_RECOVERY_TIMEOUT_SECONDS: int = 30  # time OPEN before trying HALF_OPEN
    CB_HALF_OPEN_MAX_CALLS: int = 3        # trial calls allowed in HALF_OPEN

    # ------------------------------------------------------------------
    # Downstream service registry
    # Maps a route prefix -> backend service.
    # e.g. a request to /service-a/foo gets proxied to
    #      http://service_a:8001/foo
    # ------------------------------------------------------------------
    SERVICE_REGISTRY: Dict[str, str] = {
        "service-a": "http://service_a:8001",
        "service-b": "http://service_b:8002",
        "service-c": "http://service_c:8003",
    }

    # HTTP client timeout (seconds) when proxying to backends
    PROXY_TIMEOUT_SECONDS: float = 5.0


@lru_cache
def get_settings() -> Settings:
    """
    Cached settings accessor. Using lru_cache ensures the .env file is
    parsed only once, and the same Settings instance is reused across
    the app (important since FastAPI dependencies may call this often).
    """
    return Settings()


settings = get_settings()