"""Repository base class

Provides generic CRUD operations and a unified data access layer.
"""
from abc import ABC, abstractmethod
from typing import TypeVar, Generic, Type, Optional, List
from sqlalchemy.ext.asyncio import AsyncSession

T = TypeVar('T')


class BaseRepository(Generic[T], ABC):
    """Repository base class providing generic database operations"""

    def __init__(self, session: AsyncSession, model: Type[T]):
        """Initialize the repository

        Args:
            session: Database session
            model: ORM model class
        """
        self.session = session
        self.model = model

    async def get_by_id(self, id: int) -> Optional[T]:
        """Get a record by ID

        Args:
            id: Record ID

        Returns:
            The record object, or None if it does not exist
        """
        from sqlalchemy import select

        result = await self.session.execute(
            select(self.model).where(self.model.id == id)
        )
        return result.scalar_one_or_none()

    async def get_all(self) -> List[T]:
        """Get all records

        Returns:
            List of records
        """
        from sqlalchemy import select

        result = await self.session.execute(select(self.model))
        return result.scalars().all()

    async def create(self, **kwargs) -> T:
        """Create a new record

        Args:
            **kwargs: Model field values

        Returns:
            The newly created record object
        """
        instance = self.model(**kwargs)
        self.session.add(instance)
        await self.session.flush()
        return instance

    async def update(self, id: int, **kwargs) -> Optional[T]:
        """Update a record

        Args:
            id: Record ID
            **kwargs: Field values to update

        Returns:
            The updated record object, or None if it does not exist
        """
        instance = await self.get_by_id(id)
        if instance:
            for key, value in kwargs.items():
                setattr(instance, key, value)
            await self.session.flush()
        return instance

    async def delete(self, id: int) -> bool:
        """Delete a record

        Args:
            id: Record ID

        Returns:
            True if deleted successfully, False if the record does not exist
        """
        instance = await self.get_by_id(id)
        if instance:
            await self.session.delete(instance)
            await self.session.flush()
            return True
        return False

    async def count(self) -> int:
        """Get the total number of records

        Returns:
            Number of records
        """
        from sqlalchemy import func, select

        result = await self.session.execute(
            select(func.count()).select_from(self.model)
        )
        return result.scalar()