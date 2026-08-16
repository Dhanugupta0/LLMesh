"""Queue data models - for the usage statistics queue

This module defines the data structures used in the queue, decoupling API
requests from database writes.
"""
from dataclasses import dataclass, field
from typing import Optional, Dict, Any
from datetime import datetime
from enum import Enum


class UsageEventType(str, Enum):
    """Usage event types"""
    UPDATE_USAGE = "update_usage"  # Update API usage
    UPDATE_ANTHROPIC_USAGE = "update_anthropic_usage"  # Update Anthropic usage
    INCREMENT_MODEL_REQS = "increment_model_reqs"  # Increment model request count


@dataclass
class UsageEventData:
    """Usage event data

    Encapsulates all usage statistics that need to be written to the database.
    Uses a dataclass for better performance and lower memory usage.
    """
    event_type: UsageEventType
    api_key: str
    model: Optional[str] = None
    server_url: Optional[str] = None

    # Token usage (from the upstream response)
    prompt_tokens: int = 0
    completion_tokens: int = 0

    # Weight information
    input_token_weight: float = 1.0
    output_token_weight: float = 1.0

    # Timestamp (for debugging and monitoring)
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())

    # Raw request data (for fallback token estimation)
    request_data: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to a dictionary"""
        return {
            "event_type": self.event_type,
            "api_key": self.api_key,
            "model": self.model,
            "server_url": self.server_url,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "input_token_weight": self.input_token_weight,
            "output_token_weight": self.output_token_weight,
            "timestamp": self.timestamp,
        }


@dataclass
class QueueStats:
    """Queue statistics"""
    total_enqueued: int = 0  # Total enqueued
    total_flushed: int = 0  # Total flushed
    current_queue_size: int = 0  # Current queue size
    last_flush_time: Optional[float] = None  # Last flush time
    last_flush_count: int = 0  # Last flush count
    total_errors: int = 0  # Total errors