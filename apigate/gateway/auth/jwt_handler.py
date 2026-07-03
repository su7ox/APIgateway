"""
gateway/auth/jwt_handler.py

Handles JWT creation and verification. Kept isolated from the
middleware so it's independently testable and reusable (e.g. by the
/auth/token login route AND by the auth middleware).
"""

import logging
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt

from gateway.auth.schemas import TokenPayload
from gateway.config import settings

logger = logging.getLogger("apigate.auth")


class TokenError(Exception):
    """Raised when a token is missing, malformed, expired, or invalid."""

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


def create_access_token(subject: str) -> str:
    """
    Create a signed JWT for the given subject (typically a username
    or client/service identifier).
    """
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)

    to_encode = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
    }

    token = jwt.encode(
        to_encode,
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    return token


def decode_access_token(token: str) -> TokenPayload:
    """
    Decode and validate a JWT. Raises TokenError if the token is
    invalid, malformed, or expired. `jose` automatically validates
    the `exp` claim against the current time during decode.
    """
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except JWTError as exc:
        logger.warning("JWT validation failed: %s", exc)
        raise TokenError("Invalid or expired token") from exc

    try:
        return TokenPayload(**payload)
    except Exception as exc:
        logger.warning("JWT payload shape invalid: %s", exc)
        raise TokenError("Malformed token payload") from exc


def extract_bearer_token(authorization_header: str | None) -> str:
    """
    Pull the raw token out of an `Authorization: Bearer <token>` header.
    Raises TokenError if the header is missing or malformed.
    """
    if not authorization_header:
        raise TokenError("Missing Authorization header")

    parts = authorization_header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise TokenError("Authorization header must be 'Bearer <token>'")

    return parts[1]
