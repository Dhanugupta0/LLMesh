import asyncio
from typing import Dict, Optional, List
import json
import os
import time
from datetime import datetime
from collections import OrderedDict
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, and_
from fastapi import HTTPException
from app.models.api_models import ApiKeyUsage, UsageStats
from app.utils.helpers import generate_token, get_current_time, log_api_usage
from app.config.settings import settings
from app.database.database import get_db_session
from app.database.models import ApiKey, ModelUsage, LLMServer, ServerModel
from app.database.repositories import (
    ApiKeyRepository,
    LLMServerRepository,
    ServerModelRepository,
    ModelUsageRepository,
)
import tiktoken


class ApiService:
    """API service management class"""

    def __init__(self):
        # Try to initialize the tiktoken encoding; fall back to simple character counting on failure
        try:
            self.encoding = tiktoken.encoding_for_model(settings.TOKENIZER_MODEL)
            self._use_tiktoken = True
        except Exception as e:
            # If tiktoken initialization fails (e.g. network issues), use simple character counting
            print(f"Warning: tiktoken initialization failed: {e}. Using fallback token counting.")
            self.encoding = None
            self._use_tiktoken = False

        # Improved token cache: uses an LRU cache policy
        self._token_cache = {}  # token cache
        self._token_cache_keys = []  # key list for LRU management
        self._max_token_cache_size = 1000  # max cache size

        # Model weight cache - each model has an independent cache timestamp
        # Format: {model_name: {"weights": (input_weight, output_weight), "timestamp": float}}
        self._model_weights_cache: Dict[str, dict] = {}
        self._model_weights_cache_ttl = 60  # cache TTL (seconds)

        self._stats_cache = None  # statistics cache
        self._stats_last_updated = 0

        # API Key cache - implemented with OrderedDict for LRU
        # Format: {api_key: {"valid": bool, "limit": float, "usage": float, "timestamp": float, "in_flight": int}}
        self._api_key_cache: OrderedDict[str, Dict] = OrderedDict()
        self._api_key_cache_ttl = getattr(settings, 'API_KEY_CACHE_TTL', 300)  # default 5 minutes
        self._api_key_cache_max_size = getattr(settings, 'MAX_CACHE_SIZE', 10000)  # max cache entries
        self._api_key_cache_lock = asyncio.Lock()  # thread-safety lock
        self._per_request_reserve = getattr(settings, 'PER_REQUEST_RESERVE', 5000)  # tokens reserved per in-flight request

        # Usage cache - used by check_usage_limit
        # Format: {api_key: {"usage": float, "limit": float, "timestamp": float}}
        self._usage_cache: Dict[str, Dict] = {}
        self._usage_cache_ttl = getattr(settings, 'USAGE_CACHE_TTL', 60)  # default 1 minute

    def _get_api_key_repo(self, session: AsyncSession) -> ApiKeyRepository:
        """Get an ApiKeyRepository instance"""
        return ApiKeyRepository(session, ApiKey)

    def _get_llm_server_repo(self, session: AsyncSession) -> LLMServerRepository:
        """Get an LLMServerRepository instance"""
        return LLMServerRepository(session, LLMServer)

    def _get_server_model_repo(self, session: AsyncSession) -> ServerModelRepository:
        """Get a ServerModelRepository instance"""
        return ServerModelRepository(session, ServerModel)

    def _get_model_usage_repo(self, session: AsyncSession) -> ModelUsageRepository:
        """Get a ModelUsageRepository instance"""
        return ModelUsageRepository(session, ModelUsage)

    async def validate_api_key(self, api_key: str, session: AsyncSession) -> None:
        """Validate an API key (with cache optimization)

        Prefers validation from the cache; on a cache miss, queries the
        database and writes the result to the cache.

        Args:
            api_key: the API key
            session: database session

        Raises:
            HTTPException: invalid API key
        """
        try:
            if not api_key:
                raise HTTPException(401, "Invalid API Key")

            # 1. Check the cache
            current_time = time.time()
            async with self._api_key_cache_lock:
                cached = self._api_key_cache.get(api_key)
                if cached and current_time - cached["timestamp"] < self._api_key_cache_ttl:
                    # Cache hit, move to the end (LRU)
                    self._api_key_cache.move_to_end(api_key)
                    if not cached["valid"]:
                        raise HTTPException(401, "Invalid API Key")
                    return

            # 2. Cache miss, query the database
            api_key_repo = self._get_api_key_repo(session)
            api_key_record = await api_key_repo.get_by_api_key(api_key)

            # 3. Write to the cache
            async with self._api_key_cache_lock:
                # LRU eviction
                if len(self._api_key_cache) >= self._api_key_cache_max_size:
                    self._api_key_cache.popitem(last=False)

                self._api_key_cache[api_key] = {
                    "valid": api_key_record is not None,
                    "limit": float(api_key_record.limit_value) if api_key_record else 0,  # type: ignore
                    "usage": float(api_key_record.usage) if api_key_record else 0,  # type: ignore
                    "timestamp": current_time,
                    "in_flight": 0,
                }
                self._api_key_cache.move_to_end(api_key)

            if not api_key_record:
                raise HTTPException(401, "Invalid API Key")

        except HTTPException:
            raise
        except Exception as e:
            # Log the database query error
            import logging
            logging.error(f"Database error in validate_api_key: {e}")
            raise HTTPException(500, "Internal server error during API key validation")

    async def check_usage_limit(self, api_key: str, session: AsyncSession) -> None:
        """Check the usage limit (with a concurrent reservation mechanism)

        Prefers checking from the cache; on a cache miss or when close to the
        limit, queries the database. Uses an in_flight counter to reserve quota
        and prevent concurrent requests from collectively exceeding the limit.

        Args:
            api_key: the API key
            session: database session

        Raises:
            HTTPException: usage limit exceeded
        """
        try:
            current_time = time.time()

            # 1. Check the API Key cache
            async with self._api_key_cache_lock:
                cached = self._api_key_cache.get(api_key)
                if cached and current_time - cached["timestamp"] < self._api_key_cache_ttl:
                    # Compute the effective usage (actual usage + concurrent reservation, capped at 50% of the limit)
                    reserved = min(
                        cached.get("in_flight", 0) * self._per_request_reserve,
                        cached["limit"] * 0.5
                    )
                    effective_usage = cached["usage"] + reserved
                    if effective_usage >= cached["limit"]:
                        raise HTTPException(402, "Usage limit exceeded")
                    # Usage below 90% of the limit: reserve on the cache path and return directly
                    if effective_usage < cached["limit"] * 0.9:
                        cached["in_flight"] = cached.get("in_flight", 0) + 1
                        self._api_key_cache.move_to_end(api_key)
                        return
                    # Usage >= 90%: do not increment in_flight here; handled uniformly by the DB path below

            # 2. Cache miss or close to the limit, query the database
            api_key_repo = self._get_api_key_repo(session)
            api_key_record = await api_key_repo.get_by_api_key(api_key)

            if not api_key_record:
                # API Key does not exist, let validate_api_key handle it
                return

            usage = float(api_key_record.usage)
            limit = float(api_key_record.limit_value)

            # Update the cache (including the in_flight reservation)
            async with self._api_key_cache_lock:
                if len(self._api_key_cache) >= self._api_key_cache_max_size:
                    self._api_key_cache.popitem(last=False)

                existing = self._api_key_cache.get(api_key)
                in_flight = (existing.get("in_flight", 0) if existing else 0) + 1

                self._api_key_cache[api_key] = {
                    "valid": True,
                    "limit": limit,
                    "usage": usage,
                    "timestamp": current_time,
                    "in_flight": in_flight,
                }
                self._api_key_cache.move_to_end(api_key)

            effective_usage = usage + (in_flight - 1) * self._per_request_reserve
            if effective_usage >= limit:
                raise HTTPException(402, "Usage limit exceeded")

        except HTTPException:
            raise
        except Exception as e:
            # Log the database query error
            import logging
            logging.error(f"Database error in check_usage_limit: {e}")
            raise HTTPException(500, "Internal server error during usage limit check")

    async def add_cached_usage(self, api_key: str, delta: float, count: int = 1) -> None:
        """Accumulate usage in the cache and release the concurrent reservation (write-back after billing is persisted)

        Only accumulates when the cache entry exists; also decrements the in_flight counter.

        Args:
            api_key: the API key
            delta: usage delta (weighted tokens)
            count: number of in_flight slots to release (corresponds to completed requests)
        """
        async with self._api_key_cache_lock:
            cached = self._api_key_cache.get(api_key)
            if cached is not None:
                cached["usage"] += delta
                cached["in_flight"] = max(0, cached.get("in_flight", 1) - count)

    async def release_in_flight(self, api_key: str) -> None:
        """Release one in_flight reservation (for requests that fail without billing)

        Called when check_usage_limit passed but the request ultimately
        produced no billing (upstream error, unsupported model, etc.), to
        release the reserved concurrent quota.

        Args:
            api_key: the API key
        """
        async with self._api_key_cache_lock:
            cached = self._api_key_cache.get(api_key)
            if cached is not None:
                cached["in_flight"] = max(0, cached.get("in_flight", 1) - 1)

    async def invalidate_api_key_cache(self, api_key: str) -> None:
        """Invalidate the cache for a specific API Key

        Called when a key is updated, deleted, or reset.

        Args:
            api_key: the API key
        """
        async with self._api_key_cache_lock:
            self._api_key_cache.pop(api_key, None)

    async def clear_api_key_cache(self) -> None:
        """Clear the entire API Key cache"""
        async with self._api_key_cache_lock:
            self._api_key_cache.clear()

    async def generate_api_key(self, session: AsyncSession) -> str:
        """Generate a new API key

        Args:
            session: database session

        Returns:
            str: the newly generated API key
        """
        try:
            new_key = generate_token()

            # Check whether it already exists
            api_key_repo = self._get_api_key_repo(session)
            existing = await api_key_repo.get_by_api_key(new_key)

            if existing:
                # Regenerate if it already exists
                return await self.generate_api_key(session)

            # Create a new API key record
            api_key = await api_key_repo.create(
                api_key=new_key,
                limit_value=settings.DEFAULT_LIMIT,
                created_at_str=get_current_time()
            )
            await session.commit()

            return new_key
        except Exception as e:
            # Roll back the transaction and log the error
            await session.rollback()
            import logging
            logging.error(f"Error generating API key: {e}")
            raise HTTPException(500, "Failed to generate API key")

    async def update_usage(self, api_key: str, request_data: Dict, model: str = None, session: AsyncSession = None) -> None:
        """Update API usage, computing tokens with model weights

        Args:
            api_key: the API key
            request_data: request data
            model: model name
            session: database session
        """
        if session is None:
            async for db_session in get_db_session():
                await self._update_usage_internal(api_key, request_data, model, db_session)
                return
        else:
            await self._update_usage_internal(api_key, request_data, model, session)

    async def _get_model_weights(self, model: str, session: AsyncSession, server_url: str = None) -> tuple[float, float]:
        """Get model weights, optimized with an independent cache

        Each model+server combination has an independent cache timestamp,
        avoiding wrong weights in multi-server scenarios.

        Args:
            model: model name
            session: database session
            server_url: optional server URL, to distinguish weight configs for the same model on different servers

        Returns:
            tuple: (input_weight, output_weight)
        """
        current_time = time.time()
        cache_key = f"{model}@{server_url}" if server_url else model

        # Check whether this model's cache is still valid
        if cache_key in self._model_weights_cache:
            cache_entry = self._model_weights_cache[cache_key]
            if current_time - cache_entry["timestamp"] < self._model_weights_cache_ttl:
                return cache_entry["weights"]

        # Get the model weight config from the database
        server_model_repo = self._get_server_model_repo(session)

        if server_url:
            # Look up the model weights on the server matching server_url
            llm_server_repo = self._get_llm_server_repo(session)
            server = await llm_server_repo.get_by_url(server_url)
            if server:
                server_model = await server_model_repo.get_by_server_and_frontend_name(
                    server.id, model
                )
            else:
                server_model = None
        else:
            server_model = await server_model_repo.get_by_frontend_name(model)

        # Default weights
        input_weight = 1.0
        output_weight = 1.0

        if server_model:
            input_weight = server_model.input_token_weight
            output_weight = server_model.output_token_weight

        # Update this model's independent cache
        self._model_weights_cache[cache_key] = {
            "weights": (input_weight, output_weight),
            "timestamp": current_time
        }

        return input_weight, output_weight

    async def _update_usage_internal(self, api_key: str, request_data: Dict, model: str, session: AsyncSession) -> None:
        """Internal usage update method - uses SELECT FOR UPDATE to prevent concurrent race conditions"""
        try:
            # Lock the API key record with SELECT FOR UPDATE to prevent concurrent updates
            api_key_repo = self._get_api_key_repo(session)
            api_key_record = await api_key_repo.get_for_update(api_key)

            if not api_key_record:
                return

            # Update the last used time
            api_key_record.last_used = datetime.now()
            api_key_record.last_used_str = get_current_time()
            api_key_record.reqs += 1

            # Get the model weights (cache optimized)
            input_weight = 1.0
            output_weight = 1.0

            if model:
                input_weight, output_weight = await self._get_model_weights(model, session)

            # Compute the weighted token count
            weighted_tokens = 0

            # Get the actual input and output token counts from the response
            if "usage" in request_data:
                # The request data already contains usage info (from the upstream response)
                usage_data = request_data["usage"]
                prompt_tokens = usage_data.get("prompt_tokens", 0)

                # Handle the special case of the embeddings endpoint (only prompt_tokens and total_tokens)
                if "completion_tokens" in usage_data:
                    completion_tokens = usage_data.get("completion_tokens", 0)
                elif "total_tokens" in usage_data:
                    # embeddings endpoint: total_tokens = prompt_tokens
                    completion_tokens = 0
                else:
                    completion_tokens = 0

                # Apply the weights
                weighted_tokens = (prompt_tokens * input_weight) + (completion_tokens * output_weight)
            else:
                # Fall back to estimation based on message content
                prompt_tokens = 0
                for m in request_data.get("messages", []):
                    content = m.get("content", "")
                    if isinstance(content, str):
                        # Use a stable hash cache key (hashlib.md5 is consistent across processes)
                        import hashlib
                        cache_key = hashlib.md5(content.encode('utf-8')).hexdigest()
                        if cache_key in self._token_cache:
                            prompt_tokens += self._token_cache[cache_key]
                            # Update LRU: move the recently used key to the end of the list
                            if cache_key in self._token_cache_keys:
                                self._token_cache_keys.remove(cache_key)
                            self._token_cache_keys.append(cache_key)
                        else:
                            if self._use_tiktoken and self.encoding:
                                # Use tiktoken to count tokens
                                token_count = len(self.encoding.encode(content))
                            else:
                                from app.utils.helpers import estimate_tokens_fallback
                                token_count = estimate_tokens_fallback(content)

                            # Add to the cache
                            self._token_cache[cache_key] = token_count
                            self._token_cache_keys.append(cache_key)
                            prompt_tokens += token_count

                            # Check the cache size and clean up with the LRU policy
                            if len(self._token_cache) > self._max_token_cache_size:
                                # Remove the least recently used cache entry
                                oldest_key = self._token_cache_keys.pop(0)
                                del self._token_cache[oldest_key]

                # Estimate output tokens (assumed to be 1/3 of input tokens)
                completion_tokens = max(1, int(prompt_tokens * 0.33))

                # Apply the weights
                weighted_tokens = (prompt_tokens * input_weight) + (completion_tokens * output_weight)

            api_key_record.usage += weighted_tokens

            # Update model usage statistics - also needs locking
            if model:
                model_usage_repo = self._get_model_usage_repo(session)
                model_usage = await model_usage_repo.get_for_update(api_key_record.id, model)

                if not model_usage:
                    model_usage = ModelUsage(
                        api_key_id=api_key_record.id,
                        model_name=model,
                        requests=0,
                        tokens=0
                    )
                    session.add(model_usage)

                model_usage.requests += 1
                model_usage.tokens += weighted_tokens

            await session.commit()
            # log_api_usage(api_key, api_key_record.to_dict())

        except Exception as e:
            # Roll back the transaction and log the error
            await session.rollback()
            import logging
            logging.error(f"Error updating usage for API key {api_key}: {e}")
            # Do not re-raise, to avoid affecting normal request handling

    async def get_usage_stats(self, session: AsyncSession) -> UsageStats:
        """Get usage statistics - with cache optimization

        Args:
            session: database session

        Returns:
            UsageStats: usage statistics
        """
        current_time = time.time()

        # If the cache is valid and not expired (within 5 seconds), return the cached result directly
        if self._stats_cache and current_time - self._stats_last_updated < 5:
            return self._stats_cache

        # Use the Repository to fetch the data
        api_key_repo = self._get_api_key_repo(session)
        all_api_keys = await api_key_repo.get_all()

        # Compute the statistics
        total_usage = sum(key.usage for key in all_api_keys)
        total_entries = len(all_api_keys)
        total_reqs = sum(key.reqs for key in all_api_keys)

        stats = UsageStats(
            current_time=get_current_time(),
            total_usage=total_usage,
            total_entries=total_entries,
            total_reqs=total_reqs,
        )

        # Count the number of keys in each usage bracket
        for key in all_api_keys:
            if key.usage < 100:
                stats.less_than_100 += 1
            elif key.usage < 10000:
                stats.between_100_and_10000 += 1
            else:
                stats.more_than_10000 += 1

        # Generate API key usage details
        stats.api_keys = [
            {
                "key": key.api_key[-6:],
                "phone": key.phone,
                "usage": key.usage,
                "limit": key.limit_value,
                "reqs": key.reqs,
                "created_at": key.created_at_str or (key.created_at.strftime("%Y-%m-%d %H:%M:%S") if hasattr(key.created_at, 'strftime') else str(key.created_at)),
                "last_used": key.last_used_str or (key.last_used.strftime("%Y-%m-%d %H:%M:%S") if key.last_used and hasattr(key.last_used, 'strftime') else str(key.last_used) if key.last_used else None),
            }
            for key in sorted(all_api_keys, key=lambda x: x.usage, reverse=True)
            if key.usage > 0
        ]

        # Update the cache
        self._stats_cache = stats
        self._stats_last_updated = current_time

        return stats

    async def reset_monthly_usage(self, session: AsyncSession) -> None:
        """Reset the monthly usage

        Args:
            session: database session
        """
        from sqlalchemy import delete

        # Reset the usage of all API keys - atomic operation
        await session.execute(
            update(ApiKey).values(usage=0, reqs=0)
        )

        # Reset all model usage statistics - atomic operation
        await session.execute(
            update(ModelUsage).values(requests=0, tokens=0)
        )

        await session.commit()

    async def load_llm_servers(self, session: AsyncSession) -> Dict:
        """Load the LLM server configuration

        Args:
            session: database session

        Returns:
            Dict: LLM server configuration
        """
        llm_server_repo = self._get_llm_server_repo(session)
        servers = await llm_server_repo.get_all_with_models()

        servers_dict = {}
        for server in servers:
            # Build the server config manually to avoid async lazy-loading issues
            server_config = {
                "server_url": server.server_url,
                "model": {},
                "apikey": server.apikey,
                "device": server.device,
                "enabled": True
            }

            # Build the model mapping manually - supports new and legacy fields
            for model in server.models:
                # Get the frontend model name (prefer the new field)
                frontend_name = model.frontend_model_name or model.actual_model_name
                # Get the backend model name (prefer the new field)
                backend_name = model.backend_model_name or model.client_model_name

                server_config["model"][frontend_name] = {
                    "name": backend_name,  # actual backend model name
                    "reqs": model.reqs,
                    "status": model.status,
                    "input_token_weight": model.input_token_weight,
                    "output_token_weight": model.output_token_weight
                }

            servers_dict[server.server_url] = server_config

        return servers_dict

    async def save_llm_servers(self, servers_data: Dict, session: AsyncSession) -> None:
        """Save the LLM server configuration - uses an upsert strategy to avoid brief gaps

        Compared to delete-all + insert-all, this method:
        1. Upserts each server (preserving model request counts)
        2. Only deletes old servers not in the new configuration
        3. Avoids the gap between deletion and insertion

        Args:
            servers_data: server configuration data
            session: database session
        """
        from sqlalchemy.exc import IntegrityError

        llm_server_repo = self._get_llm_server_repo(session)

        try:
            # 1. Get all existing servers
            existing_servers = await llm_server_repo.get_all_with_models()
            existing_urls = {s.server_url for s in existing_servers}
            new_urls = set(servers_data.keys())

            # 2. Delete servers that are no longer needed
            urls_to_delete = existing_urls - new_urls
            for url in urls_to_delete:
                await llm_server_repo.delete_by_url(url)

            # 3. Upsert each server
            for server_url, server_data in servers_data.items():
                await self.update_llm_server(server_url, server_data, session)

            await session.commit()

        except IntegrityError as e:
            await session.rollback()
            import logging
            logging.error(f"Database integrity error: {e}")
            raise HTTPException(400, f"Database integrity error: duplicate model configuration may exist")
        except Exception as e:
            await session.rollback()
            import logging
            logging.error(f"Error saving LLM servers: {e}")
            raise

    async def update_llm_server(self, server_url: str, server_data: Dict, session: AsyncSession) -> None:
        """Update a single LLM server configuration

        Args:
            server_url: server URL
            server_data: server configuration data
            session: database session
        """
        from sqlalchemy import delete
        from sqlalchemy.exc import IntegrityError

        llm_server_repo = self._get_llm_server_repo(session)
        server_model_repo = self._get_server_model_repo(session)

        try:
            # Use the Repository to find the existing server
            existing_server = await llm_server_repo.get_by_url_with_models(server_url)

            if existing_server:
                # Update server info
                existing_server.device = server_data.get('device', existing_server.device)
                existing_server.apikey = server_data.get('apikey', existing_server.apikey)

                # Get the new model configuration
                models_data = server_data.get('model', {})

                # Create a map of existing models to preserve request counts (supports new and legacy fields)
                existing_models_map = {}
                for model in existing_server.models:
                    # Use the frontend model name as the key, supporting new and legacy fields
                    frontend_name = model.frontend_model_name or model.actual_model_name
                    existing_models_map[frontend_name] = model

                # Delete models that no longer exist, update or add new ones
                models_to_delete = []
                for existing_model in existing_server.models:
                    frontend_name = existing_model.frontend_model_name or existing_model.actual_model_name
                    if frontend_name not in models_data:
                        models_to_delete.append(existing_model)

                # Delete models that no longer exist
                for model_to_delete in models_to_delete:
                    existing_server.models.remove(model_to_delete)
                    # Ensure deletion from the database
                    await session.delete(model_to_delete)

                # Update or add models
                for frontend_model_name, model_data in models_data.items():
                    backend_model_name = model_data.get('name', frontend_model_name)

                    if frontend_model_name in existing_models_map:
                        # Update the existing model
                        existing_model = existing_models_map[frontend_model_name]
                        # Update the legacy fields
                        existing_model.client_model_name = backend_model_name  # actual backend model name
                        existing_model.actual_model_name = frontend_model_name  # frontend model name
                        # Update the new fields
                        existing_model.backend_model_name = backend_model_name  # actual backend model name
                        existing_model.frontend_model_name = frontend_model_name  # frontend model name

                        existing_model.status = model_data.get('status', True)
                        existing_model.input_token_weight = model_data.get('input_token_weight', 1.0)
                        existing_model.output_token_weight = model_data.get('output_token_weight', 1.0)
                        # Preserve the existing request count unless a new value is explicitly given
                        if 'reqs' in model_data:
                            existing_model.reqs = model_data.get('reqs', 0)
                        # Ensure the model is marked as modified
                        session.add(existing_model)
                    else:
                        # Add a new model
                        server_model = ServerModel(
                            # Legacy fields (kept for compatibility)
                            client_model_name=backend_model_name,  # actual backend model name
                            actual_model_name=frontend_model_name,  # frontend model name
                            # New fields (clearer naming)
                            backend_model_name=backend_model_name,  # actual backend model name
                            frontend_model_name=frontend_model_name,  # frontend model name
                            reqs=model_data.get('reqs', 0),
                            status=model_data.get('status', True),
                            input_token_weight=model_data.get('input_token_weight', 1.0),
                            output_token_weight=model_data.get('output_token_weight', 1.0)
                        )
                        existing_server.models.append(server_model)
            else:
                # If the server does not exist, create a new one
                llm_server = LLMServer(
                    server_url=server_url,
                    device=server_data.get('device'),
                    apikey=server_data.get('apikey')
                )

                # Add model configurations - set both new and legacy fields
                models_data = server_data.get('model', {})
                for frontend_model_name, model_data in models_data.items():
                    backend_model_name = model_data.get('name', frontend_model_name)

                    server_model = ServerModel(
                        # Legacy fields (kept for compatibility)
                        client_model_name=backend_model_name,  # actual backend model name
                        actual_model_name=frontend_model_name,  # frontend model name
                        # New fields (clearer naming)
                        backend_model_name=backend_model_name,  # actual backend model name
                        frontend_model_name=frontend_model_name,  # frontend model name
                        reqs=model_data.get('reqs', 0),
                        status=model_data.get('status', True),
                        input_token_weight=model_data.get('input_token_weight', 1.0),
                        output_token_weight=model_data.get('output_token_weight', 1.0)
                    )
                    llm_server.models.append(server_model)

                session.add(llm_server)

            # Note: no commit inside this method; the caller commits to ensure transaction atomicity
        except IntegrityError as e:
            # Roll back the transaction
            await session.rollback()
            # Log the error and re-raise
            print(f"Database integrity error: {e}")
            raise HTTPException(400, f"Database integrity error: duplicate model configuration may exist")
        except Exception as e:
            # Roll back the transaction
            await session.rollback()
            print(f"Error updating LLM server: {e}")
            raise

    async def update_anthropic_usage(self, api_key: str, model: str, session: AsyncSession) -> None:
        """Update Anthropic API usage - only increments the request count, does not compute token usage, prevents concurrent race conditions

        Args:
            api_key: the API key
            model: model name
            session: database session
        """
        try:
            # Lock the API key record with SELECT FOR UPDATE to prevent concurrent updates
            api_key_repo = self._get_api_key_repo(session)
            model_usage_repo = self._get_model_usage_repo(session)

            api_key_record = await api_key_repo.get_for_update(api_key)

            if not api_key_record:
                return

            # Update the last used time
            api_key_record.last_used = datetime.now()
            api_key_record.last_used_str = get_current_time()

            # Only increment the request count, not the token usage
            api_key_record.reqs += 1

            # Update model usage statistics - only increment the request count, not the token usage
            if model:
                model_usage = await model_usage_repo.get_for_update(api_key_record.id, model)

                if not model_usage:
                    model_usage = ModelUsage(
                        api_key_id=api_key_record.id,
                        model_name=model,
                        requests=0,
                        tokens=0
                    )
                    session.add(model_usage)

                model_usage.requests += 1
                # tokens stays at 0 because the Anthropic route does not compute token usage

            await session.commit()

        except Exception as e:
            # Roll back the transaction and log the error
            await session.rollback()
            import logging
            logging.error(f"Error updating Anthropic usage for API key {api_key}: {e}")
            # Do not re-raise, to avoid affecting normal request handling

    async def increment_model_reqs(self, server_url: str, model_name: str, session: AsyncSession) -> None:
        """Increment the model request count

        Args:
            server_url: server URL
            model_name: model name (the frontend model name)
            session: database session
        """
        llm_server_repo = self._get_llm_server_repo(session)

        # Use the Repository to find the server
        server = await llm_server_repo.get_by_url_with_models(server_url)

        if server:
            # Find the model - supports new and legacy fields
            for server_model in server.models:
                # Check whether it matches the frontend model name (supports new and legacy fields)
                frontend_name = server_model.frontend_model_name or server_model.actual_model_name
                if frontend_name == model_name:
                    server_model.reqs += 1
                    await session.commit()
                    break