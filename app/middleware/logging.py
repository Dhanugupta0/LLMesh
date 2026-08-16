"""Request tracking middleware

Generates a unique request_id for every request and correlates it across logs.
Supports reading an existing request_id from request headers (distributed tracing).
"""
import time
import uuid
from typing import Callable
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.utils.logging_config import set_request_context, clear_request_context, get_logger

logger = get_logger(__name__)


class RequestTrackingMiddleware(BaseHTTPMiddleware):
    """Request tracking middleware

    Generates a unique request_id for every request, and:
    1. Sets it in the log context so all logs automatically include the request_id
    2. Adds it to the response headers returned to the client
    3. Supports reading an existing request_id from request headers (distributed tracing)
    """

    def __init__(self, app: ASGIApp):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Try to read request_id from request headers (distributed tracing support)
        request_id = request.headers.get("X-Request-ID") or request.headers.get("X-Trace-ID")

        # Generate a new request_id if none is present
        if not request_id:
            request_id = uuid.uuid4().hex

        # Set the request context
        set_request_context(
            request_id=request_id,
            path=request.url.path,
            method=request.method,
        )

        try:
            # Process the request
            response = await call_next(request)

            # Add the request_id to the response headers
            response.headers["X-Request-ID"] = request_id

            return response
        finally:
            # Clear the request context
            clear_request_context()


class DetailedRequestLoggingMiddleware(BaseHTTPMiddleware):
    """Request logging middleware

    Logs key information for every request:
    - Request method and path
    - Response status code
    - Request processing time
    - Client IP (for security auditing)
    """

    def __init__(self, app: ASGIApp, log_level: str = "INFO"):
        super().__init__(app)
        self.log_level = log_level.upper()

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start_time = time.time()

        # Get the client IP (for security auditing)
        client_host = request.client.host if request.client else "unknown"

        # Only log API requests (skip static assets)
        path = request.url.path
        if path.startswith("/static/") or path == "/favicon.ico":
            return await call_next(request)

        try:
            # Process the request
            response = await call_next(request)

            # Calculate the processing time
            process_time = time.time() - start_time

            # Simplified log: keep only key information
            logger.info(
                f"{request.method} {path} | {response.status_code} | {process_time:.3f}s | {client_host}"
            )

            return response

        except Exception as e:
            # Calculate the processing time
            process_time = time.time() - start_time

            # Collect key error troubleshooting information
            error_info = {
                "type": type(e).__name__,
                "message": str(e)[:200] if str(e) else "No message",
            }

            # Add extra information based on error type
            if hasattr(e, "status_code"):
                error_info["status_code"] = e.status_code

            # Add request context information (helps reproduce issues)
            context_info = {
                "query_params": dict(request.query_params) if request.query_params else None,
                "content_type": request.headers.get("content-type"),
            }

            # Build the detailed error log
            log_parts = [
                f"{request.method} {path}",
                f"ERROR",
                f"{process_time:.3f}s",
                f"{client_host}",
                f"{error_info['type']}: {error_info['message']}",
            ]

            # Add the status code (if present)
            if "status_code" in error_info:
                log_parts.insert(2, f"status={error_info['status_code']}")

            # Add context (if it helps troubleshooting)
            if context_info["query_params"]:
                log_parts.append(f"query={context_info['query_params']}")

            logger.error(" | ".join(log_parts))
            raise