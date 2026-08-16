"""Repository dependency injection factory

Provides FastAPI dependency injection functions for obtaining Repository
instances in routes.
"""
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.database import get_db_session
from app.database.models import ApiKey, LLMServer, ServerModel, ModelUsage
from app.database.repositories.api_key_repository import ApiKeyRepository
from app.database.repositories.llm_server_repository import (
    LLMServerRepository,
    ServerModelRepository,
)
from app.database.repositories.model_usage_repository import ModelUsageRepository


async def get_api_key_repo(
    session: AsyncSession = Depends(get_db_session),
) -> ApiKeyRepository:
    """Get an ApiKeyRepository instance

    Args:
        session: Database session

    Returns:
        ApiKeyRepository instance
    """
    return ApiKeyRepository(session, ApiKey)


async def get_llm_server_repo(
    session: AsyncSession = Depends(get_db_session),
) -> LLMServerRepository:
    """Get an LLMServerRepository instance

    Args:
        session: Database session

    Returns:
        LLMServerRepository instance
    """
    return LLMServerRepository(session, LLMServer)


async def get_server_model_repo(
    session: AsyncSession = Depends(get_db_session),
) -> ServerModelRepository:
    """Get a ServerModelRepository instance

    Args:
        session: Database session

    Returns:
        ServerModelRepository instance
    """
    return ServerModelRepository(session, ServerModel)


async def get_model_usage_repo(
    session: AsyncSession = Depends(get_db_session),
) -> ModelUsageRepository:
    """Get a ModelUsageRepository instance

    Args:
        session: Database session

    Returns:
        ModelUsageRepository instance
    """
    return ModelUsageRepository(session, ModelUsage)


# Export all Repository classes for direct use
__all__ = [
    "get_api_key_repo",
    "get_llm_server_repo",
    "get_server_model_repo",
    "get_model_usage_repo",
    "ApiKeyRepository",
    "LLMServerRepository",
    "ServerModelRepository",
    "ModelUsageRepository",
]