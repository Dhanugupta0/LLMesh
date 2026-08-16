"""Circuit breaker pattern implementation

Prevents cascading failures and provides fault isolation with automatic recovery.

How it works:
1. CLOSED state: normal state, all requests are forwarded normally
2. When the consecutive failure count reaches the threshold, it enters the OPEN state
3. OPEN state: fail fast, all requests immediately return an error, no forwarding
4. After recovery_timeout, it enters the HALF_OPEN state
5. HALF_OPEN state: allows a few probe requests through
   - If they succeed, it returns to normal (CLOSED)
   - If they fail, it trips again (OPEN)
"""

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional
import time

from app.utils.logging_config import get_logger, log_circuit

logger = get_logger(__name__)


class CircuitState(Enum):
    """Circuit breaker state"""
    CLOSED = "closed"           # Normal state, requests allowed
    OPEN = "open"               # Tripped state, fail fast
    HALF_OPEN = "half_open"     # Half-open state, probe requests


@dataclass
class CircuitStats:
    """Statistics for a single circuit breaker"""
    failures: int = 0                    # Consecutive failure count
    successes: int = 0                   # Success count in half-open state
    half_open_in_flight: int = 0         # In-flight probe requests in half-open state
    last_failure_time: float = 0         # Timestamp of the last failure
    state: CircuitState = CircuitState.CLOSED
    total_requests: int = 0              # Total requests (for monitoring)
    total_failures: int = 0              # Total failures (for monitoring)
    total_circuit_opens: int = 0         # Number of times tripped (for monitoring)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def to_dict(self) -> dict:
        """Export as a dictionary (for monitoring and debugging)"""
        return {
            "state": self.state.value,
            "failures": self.failures,
            "successes": self.successes,
            "last_failure_time": self.last_failure_time,
            "total_requests": self.total_requests,
            "total_failures": self.total_failures,
            "total_circuit_opens": self.total_circuit_opens,
        }


class CircuitBreaker:
    """Circuit breaker implementation

    Usage example:
        circuit_breaker = CircuitBreaker(
            failure_threshold=5,
            recovery_timeout=30.0,
            half_open_max_calls=3,
        )

        # Check whether the request is allowed
        if await circuit_breaker.can_execute("server_1"):
            try:
                result = await make_request()
                await circuit_breaker.record_success("server_1")
            except Exception:
                await circuit_breaker.record_failure("server_1")

    Configuration parameters:
        failure_threshold: how many consecutive failures trip the breaker (default 5)
        recovery_timeout: how long to wait before attempting recovery (default 30 seconds)
        half_open_max_calls: max probe requests allowed in half-open state (default 3)
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 3,
    ):
        """Initialize the circuit breaker

        Args:
            failure_threshold: consecutive failure threshold, trips when reached
            recovery_timeout: wait time before recovery attempts (seconds)
            half_open_max_calls: max probe requests in half-open state
        """
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls
        self._circuits: Dict[str, CircuitStats] = {}
        self._stats_lock = asyncio.Lock()

        logger.info(
            f"CircuitBreaker initialized | threshold={failure_threshold} | "
            f"recovery={recovery_timeout}s | probes={half_open_max_calls}"
        )

    def _get_circuit(self, key: str) -> CircuitStats:
        """Get or create a circuit breaker instance"""
        if key not in self._circuits:
            self._circuits[key] = CircuitStats()
        return self._circuits[key]

    async def can_execute(self, key: str) -> bool:
        """Check whether a request may be executed

        Args:
            key: circuit breaker identifier (usually a server URL or name)

        Returns:
            bool: True means the request is allowed, False means fail fast
        """
        circuit = self._get_circuit(key)

        async with circuit.lock:
            current_time = time.time()
            circuit.total_requests += 1

            if circuit.state == CircuitState.CLOSED:
                # Normal state, allow the request
                return True

            elif circuit.state == CircuitState.OPEN:
                # Tripped state, check whether recovery may be attempted
                time_since_failure = current_time - circuit.last_failure_time

                if time_since_failure >= self.recovery_timeout:
                    # Recovery timeout elapsed, enter half-open state
                    circuit.state = CircuitState.HALF_OPEN
                    circuit.failures = 0
                    circuit.successes = 0
                    # This request acts as the first in-flight probe
                    circuit.half_open_in_flight = 1

                    log_circuit(logger, "half_open", key, recovery_after=f"{time_since_failure:.1f}s")
                    return True

                # Still within the trip period, fail fast
                remaining = self.recovery_timeout - time_since_failure
                logger.debug(f"circuit OPEN | server={key} | remaining={remaining:.1f}s")
                return False

            elif circuit.state == CircuitState.HALF_OPEN:
                # Half-open state, limit concurrent probes by in-flight count
                if circuit.half_open_in_flight < self.half_open_max_calls:
                    circuit.half_open_in_flight += 1
                    return True

                # Max concurrent probes in half-open state reached, reject new requests
                logger.debug(f"circuit HALF_OPEN | server={key} | in_flight={circuit.half_open_in_flight}/{self.half_open_max_calls}")
                return False

        return True

    async def record_success(self, key: str):
        """Record a successful request

        Called after a request succeeds, used to restore the normal state.

        Args:
            key: circuit breaker identifier
        """
        circuit = self._get_circuit(key)

        async with circuit.lock:
            circuit.failures = 0  # Reset the consecutive failure count

            if circuit.state == CircuitState.HALF_OPEN:
                # Probe finished, release the in-flight slot
                circuit.half_open_in_flight = max(0, circuit.half_open_in_flight - 1)
                circuit.successes += 1

                if circuit.successes >= self.half_open_max_calls:
                    # Consecutive successes in half-open state, restore normal
                    circuit.state = CircuitState.CLOSED
                    circuit.successes = 0
                    circuit.half_open_in_flight = 0

                    log_circuit(logger, "close", key, reason="probes_succeeded")
            elif circuit.state == CircuitState.CLOSED:
                # Success in normal state, nothing special to do
                pass

    async def record_failure(self, key: str, error: Optional[Exception] = None):
        """Record a failed request

        Called after a request fails; may trip the breaker.

        Args:
            key: circuit breaker identifier
            error: optional exception info, used for logging
        """
        circuit = self._get_circuit(key)

        async with circuit.lock:
            circuit.failures += 1
            circuit.total_failures += 1
            circuit.last_failure_time = time.time()

            if circuit.state == CircuitState.HALF_OPEN:
                # Any failure in half-open state re-trips the breaker and resets in-flight count
                circuit.state = CircuitState.OPEN
                circuit.total_circuit_opens += 1
                circuit.successes = 0
                circuit.half_open_in_flight = 0

                log_circuit(logger, "open", key, reason="probe_failed", error=str(error)[:50] if error else None)

            elif circuit.state == CircuitState.CLOSED:
                # Normal state, check whether the threshold is reached
                if circuit.failures >= self.failure_threshold:
                    circuit.state = CircuitState.OPEN
                    circuit.total_circuit_opens += 1

                    log_circuit(logger, "open", key, failures=circuit.failures, error=str(error)[:50] if error else None)

    def get_state(self, key: str) -> CircuitState:
        """Get the current state of a specific circuit breaker (not thread-safe, monitoring only)"""
        circuit = self._get_circuit(key)
        return circuit.state

    def get_all_stats(self) -> Dict[str, dict]:
        """Get statistics for all circuit breakers (for monitoring)"""
        return {
            key: circuit.to_dict()
            for key, circuit in self._circuits.items()
        }

    def get_config(self) -> dict:
        """Get the circuit breaker configuration"""
        return {
            "failure_threshold": self.failure_threshold,
            "recovery_timeout": self.recovery_timeout,
            "half_open_max_calls": self.half_open_max_calls,
        }

    async def reset(self, key: str):
        """Manually reset a specific circuit breaker (for operations)"""
        circuit = self._get_circuit(key)

        async with circuit.lock:
            circuit.state = CircuitState.CLOSED
            circuit.failures = 0
            circuit.successes = 0
            circuit.half_open_in_flight = 0

            log_circuit(logger, "reset", key, reason="manual")

    async def reset_all(self):
        """Reset all circuit breakers"""
        for key in list(self._circuits.keys()):
            await self.reset(key)

        logger.info("All circuit breakers reset")