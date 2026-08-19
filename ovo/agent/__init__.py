"""Agent state — core state model for OVO.

Simplified: just tracks connection, model, messages, and streaming status.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Dict, Any
from dataclasses import dataclass, field
from datetime import datetime


class AgentStatus(Enum):
    """What OVO is currently doing."""
    IDLE = "idle"
    STREAMING = "streaming"
    FAILED = "failed"


@dataclass
class ChatMessage:
    """A single chat message."""
    role: str  # user | assistant | system
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    model: str = ""


@dataclass
class AgentState:
    """Complete state — single source of truth.

    The Textual UI observes this and renders accordingly.
    """

    # Status
    status: AgentStatus = AgentStatus.IDLE
    status_message: str = ""

    # Model
    model: str = ""
    provider: str = ""

    # Connection
    connected: bool = False

    # Conversation
    messages: List[ChatMessage] = field(default_factory=list)

    # Context estimate
    context_used: int = 0
    context_total: int = 128_000

    def add_message(self, role: str, content: str, **kwargs) -> ChatMessage:
        """Add a message to the conversation."""
        msg = ChatMessage(role=role, content=content, **kwargs)
        self.messages.append(msg)
        return msg

    def get_messages_for_api(self, system_prompt: str = "") -> List[Dict[str, str]]:
        """Build the message list for the LLMesh API."""
        api_msgs = []
        if system_prompt:
            api_msgs.append({"role": "system", "content": system_prompt})
        for msg in self.messages:
            if msg.role in ("user", "assistant", "system"):
                api_msgs.append({"role": msg.role, "content": msg.content})
        return api_msgs

    def clear_conversation(self):
        """Clear messages but keep model state."""
        self.messages.clear()
        self.context_used = 0
        self.status = AgentStatus.IDLE

    @property
    def message_count(self) -> int:
        return len(self.messages)
