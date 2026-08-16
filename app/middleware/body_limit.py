"""Request body size limit middleware

Prevents malicious clients from sending oversized payloads that exhaust server memory.
"""

from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp
from typing import Callable

from app.config.settings import settings


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Limit request body size

    Performs a fast check using the Content-Length header; returns 413 if the
    size exceeds the limit.
    """

    def __init__(self, app: ASGIApp, max_size: int = None):
        super().__init__(app)
        self.max_size = max_size or settings.MAX_REQUEST_BODY_SIZE

    async def dispatch(self, request: Request, call_next: Callable):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                size = int(content_length)
                if size > self.max_size:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Request body too large. "
                        f"Max: {self.max_size // (1024 * 1024)}MB, "
                        f"Received: {size // (1024 * 1024)}MB",
                    )
            except ValueError:
                pass  # Invalid content-length; let it through for downstream handling

        return await call_next(request)