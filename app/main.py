import uvicorn
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from app.core.application import create_application
from app.api.routes import router
from app.api.cli_routes import cli_router
from app.config.settings import settings

# Create the FastAPI application
app = create_application()

# Add session middleware
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SESSION_SECRET_KEY,
    max_age=settings.SESSION_MAX_AGE,
    same_site=settings.SESSION_COOKIE_SAMESITE,
    https_only=settings.SESSION_COOKIE_SECURE,
)

# Register routes
app.include_router(router)
app.include_router(cli_router)

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8087,
        reload=False,  # Disable auto-reload in production
        reload_dirs=[],  # Do not watch any directories
        log_level="info",
    )