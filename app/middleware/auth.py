from functools import wraps
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
import bcrypt
import os

from app.config.settings import settings


def user_required(func):
    """User login verification decorator"""

    @wraps(func)
    async def wrapper(request: Request, *args, **kwargs):
        if not request.session.get("user_authenticated"):
            accept = request.headers.get("accept", "")
            if "application/json" in accept:
                raise HTTPException(status_code=401, detail="User authentication required")
            return RedirectResponse(url="/", status_code=303)
        return await func(request, *args, **kwargs)

    return wrapper


def verify_user(password: str, password_hash: str) -> bool:
    """Verify a user's password"""
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except Exception:
        return False


def login_required(func):
    """Login verification decorator"""

    @wraps(func)
    async def wrapper(request: Request, *args, **kwargs):
        if not request.session.get("authenticated"):
            accept = request.headers.get("accept", "")
            if "application/json" in accept:
                raise HTTPException(status_code=401, detail="Authentication required")
            return RedirectResponse(url="/login", status_code=303)
        return await func(request, *args, **kwargs)

    return wrapper


def admin_required(func):
    """Admin verification decorator"""

    @wraps(func)
    async def wrapper(request: Request, *args, **kwargs):
        if not request.session.get("is_admin"):
            accept = request.headers.get("accept", "")
            if "application/json" in accept:
                raise HTTPException(status_code=403, detail="Admin privileges required")
            return RedirectResponse(url="/login", status_code=303)
        return await func(request, *args, **kwargs)

    return wrapper


def verify_admin(username: str, password: str) -> bool:
    """Verify admin credentials

    Uses bcrypt hash verification; the password hash is read from the
    ADMIN_PASSWORD_HASH environment variable.
    Generate a password hash with:
        python -c "import bcrypt; print(bcrypt.hashpw(b'your_password', bcrypt.gensalt()).decode())"
    """
    # Check the username
    if username != settings.ADMIN_USERNAME:
        return False

    # Check that a password hash is configured
    if not settings.ADMIN_PASSWORD_HASH:
        return False

    # Verify the password with bcrypt
    try:
        return bcrypt.checkpw(password.encode(), settings.ADMIN_PASSWORD_HASH.encode())
    except Exception:
        return False