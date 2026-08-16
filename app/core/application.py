import asyncio
import signal
import sys
from contextlib import asynccontextmanager
from typing import Set

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.models.api_models import ApiKeyUsage
from app.services.api_service import ApiService
from app.services.llm_service import LLMService
from app.services.usage_queue import UsageQueue
from app.utils.logging_config import get_logger
from app.middleware.logging import RequestTrackingMiddleware, DetailedRequestLoggingMiddleware
from app.middleware.body_limit import BodySizeLimitMiddleware
from app.database.database import get_db_session, init_db

logger = get_logger(__name__)


class Application:
    """Application core class, manages the application lifecycle and core services"""

    def __init__(self):
        self.llm_service = LLMService()
        self.api_service = ApiService()
        self.usage_queue = UsageQueue(
            batch_size=100,  # Batch write size
            flush_interval=5.0,  # 5 second flush interval
            api_service=self.api_service,  # Write back cached usage after billing is persisted
        )
        self.background_tasks: Set[asyncio.Task] = set()

    async def startup(self) -> None:
        """Application startup initialization"""
        # Initialize the database
        await init_db()

        # Initialize services
        await self.llm_service.initialize()

        # Load LLM server configuration from the database
        from app.database.database import AsyncSessionLocal
        async with AsyncSessionLocal() as session:
            await self.llm_service.init_llm_resources_from_db(session)

        # Start the usage queue worker
        await self.usage_queue.start_worker()

        # Start background tasks
        self._start_background_tasks()

    async def shutdown(self) -> None:
        """Application shutdown cleanup"""
        # Stop the usage queue worker (waits for all data to be written)
        await self.usage_queue.stop_worker()

        # Cancel background tasks
        for task in self.background_tasks:
            task.cancel()

        # Wait for tasks to complete
        if self.background_tasks:
            await asyncio.gather(*self.background_tasks, return_exceptions=True)

        # Clean up resources
        await self.llm_service.cleanup()

    def _start_background_tasks(self) -> None:
        """Start background tasks"""
        task = self._periodic_health_check_task()
        bg_task = asyncio.create_task(task)
        self.background_tasks.add(bg_task)
        bg_task.add_done_callback(self.background_tasks.discard)

    async def _periodic_health_check_task(self) -> None:
        """Periodically detect LLM server config changes and refresh

        Uses a lightweight fingerprint (model count + max ID) to detect config
        changes, and only performs a full config reload when a change is
        detected, avoiding frequent full-data queries.
        """
        from sqlalchemy import select, func
        from app.database.models import ServerModel, LLMServer

        check_interval = 15
        full_refresh_interval = settings.CACHE_TTL
        last_fingerprint = None
        last_full_refresh = 0.0

        while True:
            await asyncio.sleep(check_interval)

            try:
                async for session in get_db_session():
                    result = await session.execute(
                        select(
                            func.count(ServerModel.id),
                            func.coalesce(func.max(ServerModel.id), 0),
                            func.count(LLMServer.id),
                        )
                        .select_from(ServerModel)
                        .outerjoin(LLMServer, ServerModel.server_id == LLMServer.id)
                    )
                    row = result.one()
                    fingerprint = (row[0], row[1], row[2])

                    current_time = asyncio.get_event_loop().time()
                    need_refresh = (
                        fingerprint != last_fingerprint
                        or current_time - last_full_refresh >= full_refresh_interval
                    )

                    if need_refresh:
                        await self.llm_service.init_llm_resources_from_db(session)
                        self.llm_service.invalidate_models_cache()
                        last_fingerprint = fingerprint
                        last_full_refresh = current_time
                        logger.debug(f"Config refreshed | fingerprint={fingerprint}")
                    break
            except Exception as e:
                logger.error(f"Config check error: {e}")


def create_application() -> FastAPI:
    """Create a FastAPI application instance"""
    app = Application()

    @asynccontextmanager
    async def lifespan(fastapi_app: FastAPI):
        # Startup
        await app.startup()
        yield
        # Shutdown
        await app.shutdown()

    # Create the FastAPI application
    fastapi_app = FastAPI(lifespan=lifespan)

    # Configure middleware
    # CORS middleware must be added first (handles preflight requests)
    # Note: with allow_credentials=True you cannot use "*", you must specify concrete origins
    fastapi_app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    # Add request tracking middleware
    fastapi_app.add_middleware(RequestTrackingMiddleware)

    # Add request body size limit middleware (protects against malicious payloads)
    fastapi_app.add_middleware(BodySizeLimitMiddleware)

    # Add detailed request logging middleware (for troubleshooting)
    fastapi_app.add_middleware(DetailedRequestLoggingMiddleware)

    # Note: TrustedHostMiddleware is disabled because it causes "Invalid host header" errors
    # To enable it, uncomment the block below and configure the correct allowed_hosts
    # if settings.ENV == "production":
    #     fastapi_app.add_middleware(
    #         TrustedHostMiddleware,
    #         allowed_hosts=["*"],  # Or specify a concrete list of domains
    #     )

    # Configure static files and templates
    fastapi_app.mount(
        "/static", StaticFiles(directory=settings.STATIC_DIR), name="static"
    )

    # Register signal handling
    signal.signal(signal.SIGINT, lambda s, f: sys.exit(0))

    # Store the application instance
    fastapi_app.state.app = app

    return fastapi_app