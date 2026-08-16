"""ModelUsage data access layer

Provides database operations for the ModelUsage model.
"""
from typing import Optional, List
from sqlalchemy import select, update, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.base_repository import BaseRepository
from app.database.models import ModelUsage


class ModelUsageRepository(BaseRepository[ModelUsage]):
    """ModelUsage Repository, provides model usage statistics database operations"""

    async def get_or_create(
        self, api_key_id: int, model_name: str
    ) -> ModelUsage:
        """Get or create a model usage record

        Creates a new record if it does not exist.

        Args:
            api_key_id: API key ID
            model_name: Model name

        Returns:
            ModelUsage object
        """
        result = await self.session.execute(
            select(ModelUsage).where(
                and_(
                    ModelUsage.api_key_id == api_key_id,
                    ModelUsage.model_name == model_name,
                )
            )
        )
        usage = result.scalar_one_or_none()

        if not usage:
            usage = ModelUsage(
                api_key_id=api_key_id,
                model_name=model_name,
                requests=0,
                tokens=0
            )
            self.session.add(usage)
            await self.session.flush()

        return usage

    async def increment_usage(
        self,
        api_key_id: int,
        model_name: str,
        request_delta: int = 1,
        token_delta: float = 0,
    ) -> bool:
        """Atomically increment usage statistics

        Args:
            api_key_id: API key ID
            model_name: Model name
            request_delta: Number of requests to add
            token_delta: Number of tokens to add

        Returns:
            True if the update succeeded, False if the record does not exist
        """
        result = await self.session.execute(
            update(ModelUsage)
            .where(
                and_(
                    ModelUsage.api_key_id == api_key_id,
                    ModelUsage.model_name == model_name,
                )
            )
            .values(
                requests=ModelUsage.requests + request_delta,
                tokens=ModelUsage.tokens + token_delta,
            )
        )
        return result.rowcount > 0

    async def get_for_update(
        self, api_key_id: int, model_name: str
    ) -> Optional[ModelUsage]:
        """Get a record with a lock (SELECT FOR UPDATE)

        Used to prevent concurrent update race conditions.

        Args:
            api_key_id: API key ID
            model_name: Model name

        Returns:
            ModelUsage object, or None if it does not exist
        """
        result = await self.session.execute(
            select(ModelUsage)
            .where(
                and_(
                    ModelUsage.api_key_id == api_key_id,
                    ModelUsage.model_name == model_name,
                )
            )
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def get_by_api_key_id(self, api_key_id: int) -> List[ModelUsage]:
        """Get all model usage records for a given API key

        Args:
            api_key_id: API key ID

        Returns:
            List of ModelUsage objects
        """
        result = await self.session.execute(
            select(ModelUsage).where(ModelUsage.api_key_id == api_key_id)
        )
        return result.scalars().all()

    async def delete_by_api_key_id(self, api_key_id: int) -> int:
        """Delete all model usage records for a given API key

        Args:
            api_key_id: API key ID

        Returns:
            Number of deleted records
        """
        from sqlalchemy import delete

        result = await self.session.execute(
            delete(ModelUsage).where(ModelUsage.api_key_id == api_key_id)
        )
        return result.rowcount

    async def reset_all_by_api_key_id(self, api_key_id: int) -> int:
        """Reset all model usage statistics for a given API key

        Args:
            api_key_id: API key ID

        Returns:
            Number of updated records
        """
        result = await self.session.execute(
            update(ModelUsage)
            .where(ModelUsage.api_key_id == api_key_id)
            .values(requests=0, tokens=0)
        )
        return result.rowcount