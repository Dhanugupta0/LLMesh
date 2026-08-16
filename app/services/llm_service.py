from typing import Dict, Optional, Union, List
from collections import defaultdict
from urllib.parse import urlparse
import time
import socket
import asyncio

import httpx
from fastapi import HTTPException

from app.config.settings import settings
from app.models.api_models import AppState
from app.utils.logging_config import get_logger, log_forward, log_stream_complete, log_error
from app.utils.circuit_breaker import CircuitBreaker, CircuitState
from app.database.database import get_db_session
from app.database.models import LLMServer, ServerModel
from app.database.repositories import LLMServerRepository
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

logger = get_logger(__name__)


class LLMService:
    """LLM service management class

    Integrates the circuit breaker pattern to prevent cascading failures
    when upstream services go down.
    """

    def __init__(self):
        self.http_client: Optional[httpx.AsyncClient] = None
        self.app_state = AppState()
        self._server_health = defaultdict(lambda: {"healthy": True, "last_check": 0})
        self._server_counters = defaultdict(int)  # no await points in sync methods, safe in asyncio single thread

        # Initialize the circuit breaker
        # Configuration notes:
        # - failure_threshold: trips after 5 consecutive failures
        # - recovery_timeout: attempts recovery after 30 seconds
        # - half_open_max_calls: at most 3 probe requests in half-open state
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=5,
            recovery_timeout=30.0,
            half_open_max_calls=3,
        )

        # Connection pool adjustment lock, prevents concurrent adjustments
        self._pool_adjustment_lock = asyncio.Lock()

        # Model list cache
        self._models_cache: Optional[Dict] = None
        self._models_cache_time: float = 0
        self._models_cache_ttl = 60  # 1 minute TTL
        self._models_cache_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Initialize the HTTP client - optimized for cloud server environments"""
        self.http_client = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=500,  # reduced max connections to avoid cloud server resource limits
                max_keepalive_connections=50,  # reduced keepalive connections
                keepalive_expiry=180,  # shortened keepalive time
            ),
            timeout=httpx.Timeout(
                connect=10.0,  # 10s connect timeout
                read=None,  # no read timeout (supports long conversations)
                write=10.0,  # 10s write timeout
                pool=10.0,  # 10s pool timeout
            ),
            transport=httpx.AsyncHTTPTransport(
                retries=2,  # reduced retries to avoid latency buildup
                http2=True,  # enable HTTP/2
                socket_options=[
                    (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),  # disable Nagle's algorithm
                    (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),  # enable TCP keepalive
                ],
            ),
        )
        self._connection_pool_stats = {
            "last_check": time.time(),
            "active_connections": 0,
            "max_connections": 500,
            "adjustment_interval": 30,  # check every 30 seconds for more frequent adjustment
        }
        logger.info("HTTP client initialized | connections=500 | http2=enabled")

    async def _monitor_connection_pool(self) -> None:
        """Monitor and dynamically adjust the connection pool

        Uses a non-blocking lock check to avoid lock contention under high concurrency.
        Safely accesses httpx internals via reflection; silently degrades on version mismatch.
        """
        current_time = time.time()

        # Fast check: if the check time has not arrived, return directly
        if (
            current_time - self._connection_pool_stats["last_check"]
            <= self._connection_pool_stats["adjustment_interval"]
        ):
            return

        # Try to acquire the lock; skip this adjustment if it is held
        if self._pool_adjustment_lock.locked():
            return

        async with self._pool_adjustment_lock:
            # Double check: confirm whether adjustment is still needed after acquiring the lock
            if (
                current_time - self._connection_pool_stats["last_check"]
                <= self._connection_pool_stats["adjustment_interval"]
            ):
                return

            try:
                # Safely get the active connection count (avoid crashing on httpx private API changes)
                active_connections = 0
                try:
                    transport = getattr(self.http_client, "_transport", None)
                    if transport is not None:
                        pool = getattr(transport, "_pool", None)
                        if pool is not None and hasattr(pool, "connections"):
                            active_connections = len(pool.connections)
                except Exception:
                    pass  # silently degrade on private API changes

                if active_connections == 0:
                    self._connection_pool_stats["last_check"] = current_time
                    return

                self._connection_pool_stats["active_connections"] = active_connections

                # Dynamically adjust the connection pool size
                usage_ratio = (
                    active_connections / self._connection_pool_stats["max_connections"]
                )

                if usage_ratio > 0.8:  # usage above 80%
                    new_max = min(
                        int(self._connection_pool_stats["max_connections"] * 1.2),
                        2000,  # max 2000
                    )
                    self._connection_pool_stats["max_connections"] = new_max
                    logger.debug(f"pool expanded | connections={new_max} | usage={usage_ratio:.0%}")
                elif usage_ratio < 0.3:  # usage below 30%
                    new_max = max(
                        int(self._connection_pool_stats["max_connections"] * 0.8),
                        500,  # min 500
                    )
                    self._connection_pool_stats["max_connections"] = new_max
                    logger.debug(f"pool reduced | connections={new_max} | usage={usage_ratio:.0%}")

                self._connection_pool_stats["last_check"] = current_time

            except Exception as e:
                logger.warning(f"pool monitor error | error={e}")

    async def cleanup(self) -> None:
        """Clean up resources"""
        if self.http_client:
            await self.http_client.aclose()

    async def init_llm_resources_from_db(self, session: AsyncSession) -> None:
        """Initialize LLM resources from the database

        Args:
            session: database session
        """
        # Use the Repository to load server configurations from the database
        llm_server_repo = LLMServerRepository(session, LLMServer)
        servers = await llm_server_repo.get_all_with_models()

        servers_data = {}
        for server in servers:
            # Build the server config manually to avoid async lazy-loading issues
            server_config = {
                "server_url": server.server_url,
                "model": {},
                "apikey": server.apikey,
                "enabled": True,  # enabled by default
            }

            # Build the model mapping manually - prefer new fields, compatible with legacy fields
            for model in server.models:
                frontend_name = model.frontend_model_name or model.actual_model_name
                backend_name = model.backend_model_name or model.client_model_name

                server_config["model"][frontend_name] = {
                    "name": backend_name,
                    "input_token_weight": model.input_token_weight or 1.0,
                    "output_token_weight": model.output_token_weight or 1.0,
                }

            servers_data[server.server_url] = server_config

        self.init_llm_resources(servers_data)

    def init_llm_resources(self, servers_data: Dict) -> None:
        """Initialize LLM resources

        Args:
            servers_data: server configuration data; the model field is key-value,
                key = model name used by clients, value = actual model name forwarded
        """
        self.app_state.llm_servers = servers_data
        self.app_state.cloud_models.clear()
        self.app_state.model_mapping.clear()
        self.app_state.model_name_mapping.clear()

        for server, config in servers_data.items():
            if isinstance(config["model"], dict):
                for client_model, target_model in config["model"].items():
                    self.app_state.model_mapping[client_model].append(server)
                    # Store by server_url to avoid overwriting when multiple servers share a model name
                    self.app_state.model_name_mapping[client_model][server] = target_model
                    if "apikey" in config:
                        self.app_state.cloud_models[client_model][server] = config["apikey"]
            else:
                # Legacy format compatibility
                models = (
                    [config["model"]]
                    if isinstance(config["model"], str)
                    else config["model"]
                )
                for model in models:
                    self.app_state.model_mapping[model].append(server)
                    if "apikey" in config:
                        self.app_state.cloud_models[model][server] = config["apikey"]

    async def get_cached_models(self, session: AsyncSession) -> Dict:
        """Get the cached model list

        Uses an in-memory cache to reduce database queries, with a 1-minute TTL.

        Args:
            session: database session

        Returns:
            server configuration data
        """
        current_time = time.time()

        async with self._models_cache_lock:
            # Cache valid, return directly
            if (self._models_cache is not None and
                current_time - self._models_cache_time < self._models_cache_ttl):
                logger.debug("Models cache hit")
                return self._models_cache

        # Cache expired or missing, load from the database
        logger.debug("Models cache miss, loading from database")
        config = await self.load_llm_servers_from_db(session)

        # Update the cache
        async with self._models_cache_lock:
            self._models_cache = config
            self._models_cache_time = current_time

        return config

    def invalidate_models_cache(self) -> None:
        """Invalidate the model list cache

        Called when the LLM server configuration changes.
        """
        self._models_cache = None
        self._models_cache_time = 0
        logger.debug("Models cache invalidated")

    async def load_llm_servers_from_db(self, session: AsyncSession) -> Dict:
        """Load the LLM server configuration from the database (for caching)

        Args:
            session: database session

        Returns:
            server configuration data
        """
        llm_server_repo = LLMServerRepository(session, LLMServer)
        servers = await llm_server_repo.get_all_with_models()

        servers_data = {}
        for server in servers:
            server_config = {
                "server_url": server.server_url,
                "model": {},
                "apikey": server.apikey,
                "device": server.device,
                "enabled": True,
            }

            for model in server.models:
                frontend_name = model.frontend_model_name or model.actual_model_name
                backend_name = model.backend_model_name or model.client_model_name
                server_config["model"][frontend_name] = {
                    "name": backend_name,
                    "input_token_weight": model.input_token_weight or 1.0,
                    "output_token_weight": model.output_token_weight or 1.0,
                    "status": model.status,
                }

            servers_data[server.server_url] = server_config

        return servers_data

    def _get_healthy_servers(self, servers: List[str]) -> List[str]:
        """Get the healthy server list, with a dynamic health check interval

        Health state is keyed uniformly by the netloc extracted with
        _extract_server_key, consistent with the write key in forward_request.
        """
        current_time = time.time()
        healthy_servers = []

        for server in servers:
            health_info = self._server_health[self._extract_server_key(server)]

            # Dynamically compute the health check interval
            base_interval = 30  # base interval 30 seconds
            max_interval = 300  # max interval 5 minutes
            error_count = health_info.get("error_count", 0)
            health_check_interval = min(base_interval * (2**error_count), max_interval)

            # If past the check interval, reset the state
            if (current_time - health_info["last_check"]) > health_check_interval:
                health_info["healthy"] = True
                health_info["last_check"] = current_time  # update the check time to avoid decay on every request
                health_info["error_count"] = max(0, error_count - 1)  # gradual recovery

            # If the server is healthy, add it to the list
            if health_info["healthy"]:
                healthy_servers.append(server)

        # If there are no healthy servers, return all servers (degraded mode)
        return healthy_servers or servers

    def _update_server_health(self, server: str, is_healthy: bool) -> None:
        """Update the server health state, tracking the error count and response time"""
        health_info = self._server_health[server]
        health_info["healthy"] = is_healthy
        health_info["last_check"] = time.time()

        if not is_healthy:
            health_info["error_count"] = health_info.get("error_count", 0) + 1
        else:
            health_info["error_count"] = max(0, health_info.get("error_count", 0) - 1)

    def get_target_server(self, model: str) -> str:
        """Get the target server, using weighted round-robin load balancing

        Combines the circuit breaker state to exclude tripped servers.

        Args:
            model: model name

        Returns:
            str: target server URL

        Raises:
            HTTPException: unsupported model
        """
        servers = self.app_state.model_mapping.get(model, [])
        if not servers:
            raise HTTPException(400, f"Unsupported model: {model}")

        # Use circuit-breaker-aware health checks
        healthy_servers = self._get_healthy_servers_with_circuit_breaker(servers)

        # Compute server weights
        weights = []
        for server in healthy_servers:
            health_info = self._server_health[self._extract_server_key(server)]

            # Base weight
            weight = 100

            # Adjust the weight based on the error rate
            error_count = health_info.get("error_count", 0)
            weight -= min(error_count * 10, 50)  # 10 weight per error, max reduction 50

            # Adjust the weight based on response time (if recorded)
            if "avg_response_time" in health_info:
                response_time = health_info["avg_response_time"]
                if response_time > 1000:  # over 1 second
                    weight -= min(
                        (response_time - 1000) // 100, 30
                    )  # 1 per 100ms, max reduction 30

            weights.append(max(weight, 10))  # ensure min weight of 10

        # Select the server by weight
        total_weight = sum(weights)
        selection_point = self._server_counters[model] % total_weight
        self._server_counters[model] += 1

        # If the counter gets too large, reset it to avoid overflow
        if self._server_counters[model] > 10000:
            self._server_counters[model] = 0

        # Select the server by weight
        cumulative_weight = 0
        for i, weight in enumerate(weights):
            cumulative_weight += weight
            if selection_point < cumulative_weight:
                return healthy_servers[i]

        # If weight selection fails, fall back to round-robin
        return healthy_servers[self._server_counters[model] % len(healthy_servers)]

    def _extract_server_key(self, target: str) -> str:
        """Extract the server identifier from the target URL (for the circuit breaker key)

        Uses netloc (host:port) as the identifier, ignoring the path.
        Example: https://api.example.com/v1/chat -> api.example.com
        """
        try:
            parsed = urlparse(target)
            return parsed.netloc or target
        except Exception:
            return target

    async def forward_request(
        self, target: str, data: Dict, headers: Dict, stream: bool = False
    ) -> Union[httpx.Response, str]:
        """Forward a request to the target server

        Integrates circuit breaker protection: fails fast when the upstream
        service is down, preventing cascading errors.

        Args:
            target: target server URL
            data: request data
            headers: request headers
            stream: whether it is a streaming request

        Returns:
            response text (non-streaming) or streaming client context manager (streaming)

        Raises:
            HTTPException: 503 when the circuit breaker is OPEN; for non-streaming
                requests, 502 (upstream 5xx) / upstream 4xx status / 503 (connection
                failure) / 504 (timeout). For streaming requests, error status checks
                and circuit recording are handled by the caller (stream_wrapper).
        """
        # Extract the server identifier for the circuit breaker
        server_key = self._extract_server_key(target)

        # Circuit breaker check: fail fast if the server is tripped
        if not await self.circuit_breaker.can_execute(server_key):
            logger.warning(f"request blocked | server={server_key} | reason=circuit_open")
            raise HTTPException(
                status_code=503,
                detail=f"Service temporarily unavailable (circuit open for {server_key})"
            )

        # Monitor and adjust the connection pool
        await self._monitor_connection_pool()

        # Handle the model name mapping (find the backend model name for the routed server)
        if "model" in data and data["model"] in self.app_state.model_name_mapping:
            server_mappings = self.app_state.model_name_mapping[data["model"]]
            # Match by extracting the server URL prefix from the target URL
            for server_url, model_info in server_mappings.items():
                if target.startswith(server_url):
                    data = data.copy()
                    data["model"] = (
                        model_info if isinstance(model_info, str) else model_info["name"]
                    )
                    break

        try:
            if stream:
                # Streaming requests return the stream client directly; response status
                # checks and circuit recording are done by the caller
                # (stream_wrapper in routes) once the stream is entered
                stream_client = self.http_client.stream(
                    "POST",
                    target,
                    json=data,
                    headers=headers,
                    timeout=httpx.Timeout(
                        connect=10.0, read=None, write=10.0, pool=10.0
                    ),
                )
                return stream_client

            response = await self.http_client.post(target, json=data, headers=headers)
            response.raise_for_status()

            # Request succeeded, update the health state and circuit breaker
            self._update_server_health(server_key, True)
            await self.circuit_breaker.record_success(server_key)

            return response.text

        except httpx.HTTPStatusError as exc:
            # Server errors (5xx) trigger circuit recording
            if exc.response.status_code >= 500:
                await self.circuit_breaker.record_failure(server_key, exc)

            self._update_server_health(server_key, False)
            logger.error(f"upstream error | server={server_key} | status={exc.response.status_code}")

            # 5xx maps uniformly to 502, 4xx passes through the upstream status code; detail does not include the upstream URL
            if exc.response.status_code >= 500:
                raise HTTPException(
                    status_code=502,
                    detail=f"Upstream service returned an error (status code {exc.response.status_code})"
                )
            raise HTTPException(
                status_code=exc.response.status_code,
                detail=f"Upstream service returned an error (status code {exc.response.status_code})"
            )

        except httpx.RemoteProtocolError as exc:
            # Connection protocol error, record the failure but do not rebuild the client
            await self.circuit_breaker.record_failure(server_key, exc)
            self._update_server_health(server_key, False)
            logger.warning(f"connection reset | server={server_key} | error={str(exc)[:50]}")

            raise HTTPException(
                status_code=503,
                detail="The connection to the upstream service was interrupted, please retry"
            )

        except httpx.ConnectError as exc:
            # Connection errors (DNS resolution failure, connection refused, etc.)
            await self.circuit_breaker.record_failure(server_key, exc)
            self._update_server_health(server_key, False)
            logger.error(f"connection failed | server={server_key}")

            raise HTTPException(
                status_code=503,
                detail="Unable to connect to the upstream service"
            )

        except httpx.TimeoutException as exc:
            # Request timeout
            await self.circuit_breaker.record_failure(server_key, exc)
            self._update_server_health(server_key, False)
            logger.error(f"request timeout | server={server_key}")

            raise HTTPException(
                status_code=504,
                detail="Upstream service request timed out"
            )

        except Exception as exc:
            # Other unknown errors
            await self.circuit_breaker.record_failure(server_key, exc)
            self._update_server_health(server_key, False)
            logger.error(f"unexpected error | server={server_key} | error={str(exc)[:100]}", exc_info=True)

            raise HTTPException(
                status_code=500,
                detail="Unknown error while communicating with the upstream service"
            )

    def get_auth_header(self, model: str, api_key: str, server_url: str = None) -> Dict[str, str]:
        """Generate the authentication header

        Supports multi-server scenarios: when the same frontend model maps to
        multiple upstream servers, looks up the API key for the given server_url.

        Args:
            model: model name
            api_key: API key (as the fallback value)
            server_url: optional server URL, to look up that server's dedicated API key

        Returns:
            Dict[str, str]: authentication header
        """
        upstream_key = api_key
        if server_url and model in self.app_state.cloud_models:
            upstream_key = self.app_state.cloud_models[model].get(server_url, api_key)
        elif model in self.app_state.cloud_models:
            # Fallback compatibility: take the first available API key
            first_key = next(iter(self.app_state.cloud_models[model].values()), api_key)
            upstream_key = first_key

        return {
            "Authorization": f"Bearer {upstream_key}",
            "Content-Type": "application/json",
        }

    def _get_healthy_servers_with_circuit_breaker(self, servers: List[str]) -> List[str]:
        """Get the list of healthy servers that are not tripped

        Combines traditional health checks and circuit breaker state to filter
        available servers.

        Args:
            servers: candidate server list

        Returns:
            available server list
        """
        healthy_servers = self._get_healthy_servers(servers)
        available_servers = []

        for server in healthy_servers:
            server_key = self._extract_server_key(server)
            circuit_state = self.circuit_breaker.get_state(server_key)

            # Exclude tripped servers, keep half-open servers (allow probes)
            if circuit_state != CircuitState.OPEN:
                available_servers.append(server)
            else:
                logger.debug(f"Server {server_key} is circuit-open, skipping")

        # If there are no available servers, degrade to returning all servers (avoid total unavailability)
        return available_servers or servers

    def get_circuit_breaker_stats(self) -> Dict:
        """Get circuit breaker statistics (for monitoring)

        Returns:
            dictionary containing all circuit breaker states
        """
        return {
            "config": self.circuit_breaker.get_config(),
            "circuits": self.circuit_breaker.get_all_stats(),
        }

    async def reset_circuit_breaker(self, server_key: str = None):
        """Reset the circuit breaker state (for operations)

        Args:
            server_key: server identifier to reset; resets all if None
        """
        if server_key:
            await self.circuit_breaker.reset(server_key)
        else:
            await self.circuit_breaker.reset_all()