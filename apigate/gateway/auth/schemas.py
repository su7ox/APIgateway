"""
gateway/auth/schemas.py

Pydantic models used by the auth flow: the request body for issuing a
token, the response shape, and the decoded token payload used
internally once a request has been authenticated.
"""

from pydantic import BaseModel, Field


class TokenRequest(BaseModel):
    """Body for POST /auth/token — a minimal demo login."""

    username: str = Field(..., min_length=1, examples=["demo_user"])
    password: str = Field(..., min_length=1, examples=["demo_pass"])


class TokenResponse(BaseModel):
    """Response returned after successful authentication."""

    access_token: str
    token_type: str = "bearer"
    expires_in_minutes: int


class TokenPayload(BaseModel):
    """
    Decoded JWT claims. Attached to request.state.user by the auth
    middleware once a token has been successfully verified, so
    downstream route handlers / proxy logic can access it via
    request.state.user.sub, etc.
    """

    sub: str  # subject (username / client id)
    exp: int  # expiry (unix timestamp) — validated by jose automatically
    iat: int  # issued-at (unix timestamp)
