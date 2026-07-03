"""
gateway/dependencies.py

FastAPI dependency helpers, mainly for accessing the authenticated
user inside route handlers (as opposed to middleware, which attaches
it to request.state.user — see middleware/auth_middleware.py).
"""

from fastapi import HTTPException, Request

from gateway.auth.schemas import TokenPayload


def get_current_user(request: Request) -> TokenPayload:
    """
    Retrieve the authenticated user attached by JWTAuthMiddleware.

    Raises 401 if called on an exempt path where auth middleware never
    ran (shouldn't normally happen since exempt routes don't need
    this dependency, but guards against misuse).
    """
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user