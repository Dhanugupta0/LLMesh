"""Response cache service

Provides API response caching to reduce upstream calls for duplicate requests.
Currently only enabled for the embeddings endpoint, since completions/chat
results are not deterministic.
"""
import asyncio
import hashlib
import json
import time
from typing import Dict, Optional, Any
from collections import OrderedDict

from app.utils.logging_config import get_logger

logger = get_logger(__name__)


class ResponseCache:
    """Response cache service

    Stores API responses in an in-memory cache with LRU eviction and TTL expiry.

    Features:
    - LRU eviction policy
    - TTL expiry mechanism
    - Thread safe
    - Only caches specific endpoints (embeddings)
    """

    def __init__(self, max_size: int = 1000, ttl: int = 300):
        """Initialize the response cache

        Args:
            max_size: max number of cache entries
            ttl: cache validity period (seconds)
        """
        self._cache: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl
        self._lock = asyncio.Lock()
        self._stats = {
            "hits": 0,
            "misses": 0,
            "evictions": 0,
        }

    def _generate_key(self, request_data: Dict[str, Any]) -> str:
        """Generate a cache key

        Generates a unique key based on the request data, used to look up the cache.

        Args:
            request_data: request data

        Returns:
            the cache key (SHA256 hash)
        """
        # Normalize the request data (sort keys for consistency)
        normalized = json.dumps(request_data, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(normalized.encode()).hexdigest()

    async def get(self, request_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Get a cached response

        Args:
            request_data: request data

        Returns:
            the cached response data, or None on a miss
        """
        key = self._generate_key(request_data)

        async with self._lock:
            cached = self._cache.get(key)
            if cached:
                # Check whether it has expired
                if time.time() - cached["timestamp"] < self._ttl:
                    # Hit, move to the end (LRU)
                    self._cache.move_to_end(key)
                    self._stats["hits"] += 1
                    logger.debug(f"Response cache hit | key={key[:16]}...")
                    return cached["data"]
                else:
                    # Expired, delete
                    del self._cache[key]

            self._stats["misses"] += 1
            logger.debug(f"Response cache miss | key={key[:16]}...")
            return None

    async def set(self, request_data: Dict[str, Any], response_data: Dict[str, Any]) -> None:
        """Set the cache

        Args:
            request_data: request data
            response_data: response data
        """
        key = self._generate_key(request_data)

        async with self._lock:
            # LRU eviction
            if len(self._cache) >= self._max_size:
                oldest_key, _ = self._cache.popitem(last=False)
                self._stats["evictions"] += 1
                logger.debug(f"Response cache eviction | key={oldest_key[:16]}...")

            self._cache[key] = {
                "data": response_data,
                "timestamp": time.time(),
            }
            self._cache.move_to_end(key)
            logger.debug(f"Response cache set | key={key[:16]}...")

    async def invalidate(self, request_data: Dict[str, Any]) -> None:
        """Invalidate the cache for a specific request

        Args:
            request_data: request data
        """
        key = self._generate_key(request_data)

        async with self._lock:
            if key in self._cache:
                del self._cache[key]
                logger.debug(f"Response cache invalidated | key={key[:16]}...")

    async def clear(self) -> None:
        """Clear the entire cache"""
        async with self._lock:
            self._cache.clear()
            logger.info("Response cache cleared")

    def get_stats(self) -> Dict[str, int]:
        """Get cache statistics

        Returns:
            statistics dictionary
        """
        total = self._stats["hits"] + self._stats["misses"]
        hit_rate = self._stats["hits"] / total if total > 0 else 0

        return {
            "hits": self._stats["hits"],
            "misses": self._stats["misses"],
            "evictions": self._stats["evictions"],
            "hit_rate": round(hit_rate, 4),
            "size": len(self._cache),
            "max_size": self._max_size,
            "ttl": self._ttl,
        }


# Global response cache instance
response_cache = ResponseCache(max_size=1000, ttl=300)