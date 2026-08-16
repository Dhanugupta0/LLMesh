import os
import logging
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import event, text
from app.config.settings import settings
from app.database.models import Base

logger = logging.getLogger(__name__)

# Database file path - stored under the app/database directory
DATABASE_PATH = os.path.join(settings.BASE_DIR, 'app', 'database', 'myapi.db')
DATABASE_URL = f"sqlite+aiosqlite:///{DATABASE_PATH}"

# Create the async engine
# SQLite connection pool configuration:
# - pool_pre_ping: connection health check, prevents using stale connections
# - pool_recycle: connection recycle time, avoids holding connections too long
# - connect_args: SQLite-specific settings
# Note: when migrating to PostgreSQL in the future, add pool_size and max_overflow
engine = create_async_engine(
    DATABASE_URL,
    echo=False,  # Set to False in production
    future=True,
    pool_pre_ping=True,  # Verify connection validity before each use
    pool_recycle=3600,  # Recycle connections after 1 hour to prevent long idle
    connect_args={
        "check_same_thread": False,  # Allow multi-thread access (required for FastAPI async)
    },
)


@event.listens_for(engine.sync_engine, "connect")
def set_sqlite_pragma(dbapi_conn, connection_record):
    """Set SQLite performance optimization parameters

    Enable WAL mode to improve concurrent performance:
    - WAL (Write-Ahead Logging) allows reads and writes to run concurrently
    - Writes do not block reads, suitable for read-heavy workloads
    - Improves concurrent throughput
    """
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")  # Balance performance and data safety
    cursor.execute("PRAGMA cache_size=10000")  # Increase cache pages
    cursor.execute("PRAGMA temp_store=MEMORY")  # Store temp tables in memory
    cursor.close()

# Create the async session factory
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db_session() -> AsyncSession:
    """Get a database session"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db():
    """Initialize the database, creating all tables"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Add a unique index on phone for existing databases (new DBs already
        # get the constraint from create_all). If existing duplicate phone
        # numbers prevent creation, log a warning without aborting startup.
        try:
            await conn.execute(
                text("CREATE UNIQUE INDEX IF NOT EXISTS ix_api_keys_phone ON api_keys (phone)")
            )
        except Exception as e:
            logger.warning(f"Failed to create unique index on api_keys.phone (duplicate phone numbers may exist): {e}")


async def close_db():
    """Close the database connection"""
    await engine.dispose()