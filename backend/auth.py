from fastapi import HTTPException, Request

from .session import verify_session


def require_auth(request: Request):
    token = request.cookies.get("session")
    if not token:
        raise HTTPException(401, "Not authenticated")
    user = verify_session(token)
    if not user:
        raise HTTPException(401, "Invalid session")
    return user


def require_admin(request: Request):
    user = require_auth(request)
    if user["role"] != "admin":
        raise HTTPException(403, "Admin access required")
    return user
