"""Usage statistics queue service

Uses an in-memory queue + background worker pattern to decouple API requests
from database writes:
1. Usage data is enqueued when the API request finishes (non-blocking)
2. The background worker batches the data and flushes it to the database on a timer
3. Graceful shutdown ensures all data is written

Performance improvements:
- Streaming responses no longer block waiting for database writes
- Batch writes reduce the number of database operations
- Higher database connection reuse
"""
import asyncio
from collections import defaultdict
from typing import Optional, List, Dict, Any, TYPE_CHECKING
from datetime import datetime
import logging

if TYPE_CHECKING:
    from app.services.api_service import ApiService

from app.models.queue_models import (
    UsageEventData,
    UsageEventType,
    QueueStats,
)
from app.database.database import AsyncSessionLocal
from app.database.models import ApiKey, ModelUsage, LLMServer
from app.database.repositories import (
    ApiKeyRepository,
    ModelUsageRepository,
    LLMServerRepository,
)
from sqlalchemy import select, and_
from sqlalchemy.orm import selectinload
from app.utils.logging_config import get_logger

logger = get_logger(__name__)


class UsageQueue:
    """Usage statistics queue service

    Workflow:
    1. API requests enqueue usage data via enqueue()
    2. The background worker _worker() continuously pulls data from the queue
    3. A batch write is triggered when batch_size or flush_interval is reached
    4. stop_worker() is called at application shutdown to ensure all data is written
    """

    def __init__(
        self,
        batch_size: int = 100,
        flush_interval: float = 5.0,
        api_service: Optional["ApiService"] = None,
    ):
        """Initialize the queue service

        Args:
            batch_size: batch write size; flush immediately when reached
            flush_interval: flush interval (seconds); triggers a flush when exceeded
            api_service: optional ApiService reference; writes back the cached usage
                after billing is persisted; skips the write-back when None (the queue
                can be instantiated independently)
        """
        self.queue: asyncio.Queue[UsageEventData] = asyncio.Queue()
        self.batch: List[UsageEventData] = []
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self._api_service = api_service

        # Worker control
        self._worker_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._started = False

        # Statistics
        self.stats = QueueStats()

        # Consecutive flush failure counter (prevents infinite retries on persistent errors)
        self._consecutive_failures = 0
        self._max_consecutive_failures = 3

        # Buffer grouped by event type (optimizes batch writes)
        self._grouped_buffer: Dict[UsageEventType, List[UsageEventData]] = defaultdict(
            list
        )

    async def enqueue(self, event_data: UsageEventData) -> None:
        """Enqueue usage event data

        Non-blocking operation, returns immediately.

        Args:
            event_data: usage event data
        """
        await self.queue.put(event_data)
        self.stats.total_enqueued += 1

    async def start_worker(self) -> None:
        """Start the background worker

        Should be called at application startup.
        """
        if self._started:
            logger.warning("UsageQueue already running")
            return

        self._started = True
        self._stop_event.clear()
        self._worker_task = asyncio.create_task(self._worker())
        logger.info(f"UsageQueue started | batch={self.batch_size} | interval={self.flush_interval}s")

    async def stop_worker(self) -> None:
        """Stop the worker and flush the remaining data

        Should be called at application shutdown. Blocks until all data is written.
        """
        if not self._started:
            return

        logger.info("UsageQueue stopping...")

        # Send the stop signal
        self._stop_event.set()

        # Give the worker a graceful exit chance (with a timeout), then cancel
        if self._worker_task and not self._worker_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._worker_task), timeout=3.0)
            except asyncio.TimeoutError:
                self._worker_task.cancel()
                try:
                    await self._worker_task
                except asyncio.CancelledError:
                    pass

        # Drain all events stuck in the queue into the grouped buffer, avoiding silent drops
        drained = 0
        while True:
            try:
                event_data = self.queue.get_nowait()
                self._grouped_buffer[event_data.event_type].append(event_data)
                drained += 1
            except asyncio.QueueEmpty:
                break
        if drained:
            logger.info(f"UsageQueue drain | remaining={drained}")

        # Flush the remaining data
        if self._grouped_buffer:
            remaining = sum(len(v) for v in self._grouped_buffer.values())
            logger.info(f"UsageQueue flush | remaining={remaining}")
            await self._flush_to_database()

        self._started = False
        logger.info("UsageQueue stopped")

    async def _worker(self) -> None:
        """Background worker coroutine

        Continuously pulls data from the queue and groups it by event type,
        triggering a batch write when the conditions are met.
        """
        last_flush = asyncio.get_event_loop().time()

        while not self._stop_event.is_set():
            try:
                # Use a timeout to periodically check flush_interval
                event_data = await asyncio.wait_for(
                    self.queue.get(), timeout=self.flush_interval
                )

                # Group by event type
                self._grouped_buffer[event_data.event_type].append(event_data)
                self.stats.current_queue_size = self.queue.qsize()

                # Check whether a flush is needed
                total_buffered = sum(len(v) for v in self._grouped_buffer.values())
                current_time = asyncio.get_event_loop().time()
                elapsed = current_time - last_flush

                if total_buffered >= self.batch_size or elapsed >= self.flush_interval:
                    await self._flush_to_database()
                    last_flush = current_time

            except asyncio.TimeoutError:
                # Timeout triggers a flush
                total_buffered = sum(len(v) for v in self._grouped_buffer.values())
                if total_buffered > 0:
                    await self._flush_to_database()
                    last_flush = asyncio.get_event_loop().time()

            except Exception as e:
                logger.error(f"worker error | error={str(e)[:100]}", exc_info=True)
                self.stats.total_errors += 1
                # Keep running, do not stop on errors

    async def _flush_to_database(self) -> None:
        """Batch flush to the database

        Groups the buffered data by event type and writes it to the database in bulk.
        """
        if not self._grouped_buffer:
            return

        start_time = asyncio.get_event_loop().time()
        total_events = sum(len(v) for v in self._grouped_buffer.values())

        try:
            async with AsyncSessionLocal() as session:
                # Process UPDATE_USAGE events
                if UsageEventType.UPDATE_USAGE in self._grouped_buffer:
                    await self._process_update_usage(
                        session, self._grouped_buffer[UsageEventType.UPDATE_USAGE]
                    )

                # Process UPDATE_ANTHROPIC_USAGE events
                if UsageEventType.UPDATE_ANTHROPIC_USAGE in self._grouped_buffer:
                    await self._process_update_anthropic_usage(
                        session,
                        self._grouped_buffer[UsageEventType.UPDATE_ANTHROPIC_USAGE],
                    )

                # Process INCREMENT_MODEL_REQS events
                if UsageEventType.INCREMENT_MODEL_REQS in self._grouped_buffer:
                    await self._process_increment_model_reqs(
                        session,
                        self._grouped_buffer[UsageEventType.INCREMENT_MODEL_REQS],
                    )

                await session.commit()

            # Commit succeeded - clear the buffer and reset the failure counter
            self._grouped_buffer.clear()
            self._consecutive_failures = 0

            # Update the statistics
            self.stats.total_flushed += total_events
            self.stats.last_flush_count = total_events
            self.stats.last_flush_time = start_time

            elapsed = asyncio.get_event_loop().time() - start_time
            # Only log in debug mode or abnormal situations (max prevents division by zero)
            rate = total_events / max(elapsed, 1e-6)
            if rate < 100 or elapsed > 1.0:
                logger.warning(f"flush slow | events={total_events} | duration={elapsed:.3f}s | rate={rate:.0f}/s")
            else:
                logger.debug(f"flush ok | events={total_events} | duration={elapsed:.3f}s | rate={rate:.0f}/s")

        except Exception as e:
            self._consecutive_failures += 1
            logger.error(
                f"flush failed | events={total_events} | "
                f"error={str(e)[:100]} | attempt={self._consecutive_failures}/{self._max_consecutive_failures}",
                exc_info=True,
            )
            self.stats.total_errors += 1

            # Drop the buffered data after exceeding the max consecutive failures to prevent infinite retries
            if self._consecutive_failures > self._max_consecutive_failures:
                discarded = sum(len(v) for v in self._grouped_buffer.values())
                logger.critical(
                    f"flush abandoned after {self._max_consecutive_failures} failures | "
                    f"discarded={discarded} events"
                )
                self._grouped_buffer.clear()
                self._consecutive_failures = 0
            # Otherwise the data stays in the buffer and is retried next time

    async def _process_update_usage(
        self, session: AsyncSessionLocal, events: List[UsageEventData]
    ) -> None:
        """Batch process UPDATE_USAGE events

        Optimization strategy:
        1. Group by api_key and aggregate all updates for the same API key
        2. Use SELECT FOR UPDATE to prevent concurrency issues
        """
        # Create Repository instances
        api_key_repo = ApiKeyRepository(session, ApiKey)
        model_usage_repo = ModelUsageRepository(session, ModelUsage)

        # Group by api_key
        grouped_by_key: Dict[str, List[UsageEventData]] = defaultdict(list)
        for event in events:
            grouped_by_key[event.api_key].append(event)

        for api_key, key_events in grouped_by_key.items():
            # Lock the API key record
            api_key_record = await api_key_repo.get_for_update(api_key)

            if not api_key_record:
                continue

            # Update the basic fields
            total_weighted_tokens = 0
            api_key_record.reqs += len(key_events)
            api_key_record.last_used = datetime.now()
            api_key_record.last_used_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Process grouped by model
            model_updates: Dict[str, Dict[str, float]] = defaultdict(
                lambda: {"tokens": 0, "requests": 0}
            )

            for event in key_events:
                # Compute the weighted tokens
                weighted = (
                    event.prompt_tokens * event.input_token_weight
                    + event.completion_tokens * event.output_token_weight
                )
                total_weighted_tokens += weighted

                if event.model:
                    model_updates[event.model]["tokens"] += weighted
                    model_updates[event.model]["requests"] += 1

            api_key_record.usage += total_weighted_tokens

            # Write back the cached usage so quota checks are not based on stale snapshots
            if self._api_service is not None:
                await self._api_service.add_cached_usage(api_key, total_weighted_tokens, count=len(key_events))

            # Update the model usage statistics
            for model_name, model_data in model_updates.items():
                model_usage = await model_usage_repo.get_for_update(api_key_record.id, model_name)

                if not model_usage:
                    model_usage = ModelUsage(
                        api_key_id=api_key_record.id,
                        model_name=model_name,
                        requests=model_data["requests"],
                        tokens=model_data["tokens"],
                    )
                    session.add(model_usage)
                else:
                    model_usage.requests += model_data["requests"]
                    model_usage.tokens += model_data["tokens"]

    async def _process_update_anthropic_usage(
        self, session: AsyncSessionLocal, events: List[UsageEventData]
    ) -> None:
        """Batch process UPDATE_ANTHROPIC_USAGE events

        Anthropic endpoint usage formula:
        usage = requests × max(input_weight, output_weight)
        """
        # Create Repository instances
        api_key_repo = ApiKeyRepository(session, ApiKey)
        model_usage_repo = ModelUsageRepository(session, ModelUsage)

        # Group by api_key
        grouped_by_key: Dict[str, List[UsageEventData]] = defaultdict(list)
        for event in events:
            grouped_by_key[event.api_key].append(event)

        for api_key, key_events in grouped_by_key.items():
            api_key_record = await api_key_repo.get_for_update(api_key)

            if not api_key_record:
                continue

            # Update the request count and time
            api_key_record.reqs += len(key_events)
            api_key_record.last_used = datetime.now()
            api_key_record.last_used_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Compute the usage grouped by model
            # Each event: usage = 1 × max(input_weight, output_weight)
            model_updates: Dict[str, Dict[str, float]] = defaultdict(
                lambda: {"tokens": 0, "requests": 0}
            )

            for event in key_events:
                if event.model:
                    # Compute the usage of a single request
                    usage_per_request = max(event.input_token_weight, event.output_token_weight)
                    model_updates[event.model]["tokens"] += usage_per_request
                    model_updates[event.model]["requests"] += 1

            # Compute the total usage
            total_usage = sum(m["tokens"] for m in model_updates.values())
            api_key_record.usage += total_usage

            # Write back the cached usage so quota checks are not based on stale snapshots
            if self._api_service is not None:
                await self._api_service.add_cached_usage(api_key, total_usage, count=len(key_events))

            # Update the model usage statistics
            for model_name, model_data in model_updates.items():
                model_usage = await model_usage_repo.get_for_update(api_key_record.id, model_name)

                if not model_usage:
                    model_usage = ModelUsage(
                        api_key_id=api_key_record.id,
                        model_name=model_name,
                        requests=int(model_data["requests"]),
                        tokens=model_data["tokens"],
                    )
                    session.add(model_usage)
                else:
                    model_usage.requests += int(model_data["requests"])
                    model_usage.tokens += model_data["tokens"]

    async def _process_increment_model_reqs(
        self, session: AsyncSessionLocal, events: List[UsageEventData]
    ) -> None:
        """Batch process INCREMENT_MODEL_REQS events

        Updates the request counts of server models.
        Optimization: preloads all servers in bulk to avoid repeated queries.
        """
        # Create Repository instances
        llm_server_repo = LLMServerRepository(session, LLMServer)

        # Group by server_url + model
        grouped: Dict[tuple, int] = defaultdict(int)
        for event in events:
            if event.server_url and event.model:
                grouped[(event.server_url, event.model)] += 1

        if not grouped:
            return

        # Bulk load all needed servers (optimization: one query fetches all servers)
        server_urls = set(server_url for (server_url, _) in grouped.keys())

        # Fetch all servers in one query
        all_servers = await llm_server_repo.get_all_with_models()
        server_map = {server.server_url: server for server in all_servers}

        # Update the model request counts
        for (server_url, model_name), count in grouped.items():
            server = server_map.get(server_url)

            if server:
                for server_model in server.models:
                    frontend_name = (
                        server_model.frontend_model_name
                        or server_model.actual_model_name
                    )
                    if frontend_name == model_name:
                        server_model.reqs += count
                        break

    def get_stats(self) -> Dict[str, Any]:
        """Get queue statistics"""
        return {
            "total_enqueued": self.stats.total_enqueued,
            "total_flushed": self.stats.total_flushed,
            "current_queue_size": self.queue.qsize(),
            "current_buffer_size": sum(len(v) for v in self._grouped_buffer.values()),
            "last_flush_time": self.stats.last_flush_time,
            "last_flush_count": self.stats.last_flush_count,
            "total_errors": self.stats.total_errors,
            "started": self._started,
        }