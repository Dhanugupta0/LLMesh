"""ApiKey data access layer

Provides database operations for the ApiKey model.
"""
from typing import Optional, List
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.base_repository import BaseRepository
from app.database.models import ApiKey


class ApiKeyRepository(BaseRepository[ApiKey]):
    """ApiKey Repository, provides API key related database operations"""

    async def get_by_api_key(self, api_key: str) -> Optional[ApiKey]:
        """Query a record by API key

        Args:
            api_key: The API key string

        Returns:
            ApiKey object, or None if it does not exist
        """
        result = await self.session.execute(
            select(ApiKey).where(ApiKey.api_key == api_key)
        )
        return result.scalar_one_or_none()

    async def get_by_phone(self, phone: str) -> Optional[ApiKey]:
        """Query a record by phone number

        Args:
            phone: Phone number

        Returns:
            ApiKey object, or None if it does not exist
        """
        result = await self.session.execute(
            select(ApiKey).where(ApiKey.phone == phone)
        )
        return result.scalar_one_or_none()

    async def get_by_phone_with_usages(self, phone: str) -> Optional[ApiKey]:
        """Query a record by phone number, preloading the model_usages relationship

        Args:
            phone: Phone number

        Returns:
            ApiKey object (including model_usages), or None if it does not exist
        """
        result = await self.session.execute(
            select(ApiKey)
            .options(selectinload(ApiKey.model_usages))
            .where(ApiKey.phone == phone)
        )
        return result.scalar_one_or_none()

    async def update_usage(self, api_key: str, usage_delta: float) -> bool:
        """Atomically increment the usage field

        Uses SQLAlchemy atomic operations to avoid concurrency issues.

        Args:
            api_key: API key
            usage_delta: The usage amount to add

        Returns:
            True if the update succeeded, False if the record does not exist
        """
        result = await self.session.execute(
            update(ApiKey)
            .where(ApiKey.api_key == api_key)
            .values(usage=ApiKey.usage + usage_delta)
        )
        return result.rowcount > 0

    async def increment_reqs(self, api_key: str) -> bool:
        """Atomically increment the reqs field

        Args:
            api_key: API key

        Returns:
            True if the update succeeded, False if the record does not exist
        """
        result = await self.session.execute(
            update(ApiKey)
            .where(ApiKey.api_key == api_key)
            .values(reqs=ApiKey.reqs + 1)
        )
        return result.rowcount > 0

    async def reset_usage(self, api_key: str) -> bool:
        """Reset the usage and reqs fields

        Args:
            api_key: API key

        Returns:
            True if the update succeeded, False if the record does not exist
        """
        result = await self.session.execute(
            update(ApiKey)
            .where(ApiKey.api_key == api_key)
            .values(usage=0, reqs=0)
        )
        return result.rowcount > 0

    async def update_limit(self, api_key: str, new_limit: float) -> bool:
        """Update the usage limit

        Args:
            api_key: API key
            new_limit: The new limit value

        Returns:
            True if the update succeeded, False if the record does not exist
        """
        result = await self.session.execute(
            update(ApiKey)
            .where(ApiKey.api_key == api_key)
            .values(limit_value=new_limit)
        )
        return result.rowcount > 0

    async def delete_by_api_key(self, api_key: str) -> bool:
        """Delete a record by API key

        Args:
            api_key: API key

        Returns:
            True if deleted successfully, False if the record does not exist
        """
        # Query the object first, then delete (ensures cascade deletes work)
        instance = await self.get_by_api_key(api_key)
        if instance:
            await self.session.delete(instance)
            await self.session.flush()
            return True
        return False

    async def get_all_with_usages(self) -> List[ApiKey]:
        """Get all API keys, including the model_usages relationship

        Returns:
            List of ApiKey objects with model_usages preloaded
        """
        result = await self.session.execute(
            select(ApiKey).options(selectinload(ApiKey.model_usages))
        )
        return result.scalars().all()

    async def get_all(self) -> List[ApiKey]:
        """Get all API keys

        Returns:
            List of ApiKey objects
        """
        result = await self.session.execute(select(ApiKey))
        return result.scalars().all()

    async def get_for_update(self, api_key: str) -> Optional[ApiKey]:
        """Get a record with a lock (SELECT FOR UPDATE)

        Used to prevent concurrent update race conditions.

        Args:
            api_key: API key

        Returns:
            ApiKey object, or None if it does not exist
        """
        result = await self.session.execute(
            select(ApiKey)
            .where(ApiKey.api_key == api_key)
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def update_last_used(self, api_key: str, last_used_str: str) -> bool:
        """Update the last used time

        Args:
            api_key: API key
            last_used_str: Time string

        Returns:
            True if the update succeeded, False if the record does not exist
        """
        from datetime import datetime

        result = await self.session.execute(
            update(ApiKey)
            .where(ApiKey.api_key == api_key)
            .values(last_used=datetime.now(), last_used_str=last_used_str)
        )
        return result.rowcount > 0