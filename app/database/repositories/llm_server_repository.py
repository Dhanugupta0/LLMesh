"""LLMServer and ServerModel data access layer

Provides database operations for LLM servers and server models.
"""
from typing import Optional, List, Dict
from sqlalchemy import select, update, delete, or_
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.base_repository import BaseRepository
from app.database.models import LLMServer, ServerModel


class LLMServerRepository(BaseRepository[LLMServer]):
    """LLMServer Repository, provides LLM server related database operations"""

    async def get_by_url(self, server_url: str) -> Optional[LLMServer]:
        """Query a record by server URL

        Args:
            server_url: Server URL

        Returns:
            LLMServer object, or None if it does not exist
        """
        result = await self.session.execute(
            select(LLMServer).where(LLMServer.server_url == server_url)
        )
        return result.scalar_one_or_none()

    async def get_all_with_models(self) -> List[LLMServer]:
        """Get all servers, including the models relationship

        Returns:
            List of LLMServer objects with models preloaded
        """
        result = await self.session.execute(
            select(LLMServer).options(selectinload(LLMServer.models))
        )
        return result.scalars().all()

    async def get_by_url_with_models(self, server_url: str) -> Optional[LLMServer]:
        """Get a server by URL, including the models relationship

        Args:
            server_url: Server URL

        Returns:
            LLMServer object with models preloaded, or None if it does not exist
        """
        result = await self.session.execute(
            select(LLMServer)
            .where(LLMServer.server_url == server_url)
            .options(selectinload(LLMServer.models))
        )
        return result.scalar_one_or_none()

    async def delete_by_url(self, server_url: str) -> bool:
        """Delete a server by URL

        Args:
            server_url: Server URL

        Returns:
            True if deleted successfully, False if the record does not exist
        """
        instance = await self.get_by_url(server_url)
        if instance:
            await self.session.delete(instance)
            await self.session.flush()
            return True
        return False

    async def delete_all(self) -> int:
        """Delete all server configurations

        Note: this cascades and deletes all associated ServerModel records.

        Returns:
            Number of deleted records
        """
        # First delete all ServerModel records
        await self.session.execute(delete(ServerModel))
        # Then delete all LLMServer records
        result = await self.session.execute(delete(LLMServer))
        return result.rowcount

    async def get_all(self) -> List[LLMServer]:
        """Get all servers

        Returns:
            List of LLMServer objects
        """
        result = await self.session.execute(select(LLMServer))
        return result.scalars().all()


class ServerModelRepository(BaseRepository[ServerModel]):
    """ServerModel Repository, provides server model related database operations"""

    async def increment_reqs(self, model_id: int) -> bool:
        """Atomically increment the reqs field

        Args:
            model_id: Model ID

        Returns:
            True if the update succeeded, False if the record does not exist
        """
        result = await self.session.execute(
            update(ServerModel)
            .where(ServerModel.id == model_id)
            .values(reqs=ServerModel.reqs + 1)
        )
        return result.rowcount > 0

    async def get_by_frontend_name(self, model_name: str) -> Optional[ServerModel]:
        """Query by frontend model name

        Supports compatibility with both new and legacy fields.

        Args:
            model_name: The model name used by the frontend

        Returns:
            ServerModel object, or None if it does not exist
        """
        result = await self.session.execute(
            select(ServerModel).where(
                or_(
                    ServerModel.actual_model_name == model_name,  # Legacy field
                    ServerModel.frontend_model_name == model_name  # New field
                )
            ).options(selectinload(ServerModel.server))
        )
        return result.scalar_one_or_none()

    async def get_by_server_and_frontend_name(
        self, server_id: int, model_name: str
    ) -> Optional[ServerModel]:
        """Query by server ID and frontend model name

        Args:
            server_id: Server ID
            model_name: The model name used by the frontend

        Returns:
            ServerModel object, or None if it does not exist
        """
        result = await self.session.execute(
            select(ServerModel).where(
                ServerModel.server_id == server_id,
                or_(
                    ServerModel.actual_model_name == model_name,  # Legacy field
                    ServerModel.frontend_model_name == model_name  # New field
                )
            )
        )
        return result.scalar_one_or_none()

    async def find_by_server_url_and_model(
        self, server_url: str, model_name: str, session: AsyncSession
    ) -> Optional[ServerModel]:
        """Find a model by server URL and frontend model name

        Args:
            server_url: Server URL
            model_name: The model name used by the frontend
            session: Database session

        Returns:
            ServerModel object, or None if it does not exist
        """
        # First get the server
        result = await session.execute(
            select(LLMServer)
            .where(LLMServer.server_url == server_url)
            .options(selectinload(LLMServer.models))
        )
        server = result.scalar_one_or_none()

        if not server:
            return None

        # Look through the server's models
        for server_model in server.models:
            frontend_name = (
                server_model.frontend_model_name or server_model.actual_model_name
            )
            if frontend_name == model_name:
                return server_model

        return None

    async def delete_by_server_id(self, server_id: int) -> int:
        """Delete all models of a given server

        Args:
            server_id: Server ID

        Returns:
            Number of deleted records
        """
        result = await self.session.execute(
            delete(ServerModel).where(ServerModel.server_id == server_id)
        )
        return result.rowcount