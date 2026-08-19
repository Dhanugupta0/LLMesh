"""OVO Session Manager — local-only session persistence.

Sessions are stored as JSON files in ~/.ovo/sessions/.
Nothing is sent to the server. Only conversation content and model name are persisted.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from dataclasses import dataclass, field, asdict

from ovo.config import SESSIONS_DIR


@dataclass
class SessionMessage:
    """A single message in a session."""
    role: str
    content: str
    timestamp: str = ""
    model: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


@dataclass
class Session:
    """A saved conversation session."""
    id: str = ""
    title: str = "New session"
    model: str = ""
    created_at: str = ""
    updated_at: str = ""
    messages: List[SessionMessage] = field(default_factory=list)

    def __post_init__(self):
        if not self.id:
            self.id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now

    @property
    def message_count(self) -> int:
        return len(self.messages)

    @property
    def preview(self) -> str:
        """First user message as a preview, truncated."""
        for msg in self.messages:
            if msg.role == "user":
                text = msg.content.strip().replace("\n", " ")
                return text[:60] + ("…" if len(text) > 60 else "")
        return "Empty session"

    @property
    def age_label(self) -> str:
        """Human-readable age like '2 hours ago'."""
        try:
            updated = datetime.fromisoformat(self.updated_at)
            now = datetime.now(timezone.utc)
            delta = now - updated
            seconds = delta.total_seconds()

            if seconds < 60:
                return "just now"
            elif seconds < 3600:
                mins = int(seconds // 60)
                return f"{mins}m ago"
            elif seconds < 86400:
                hours = int(seconds // 3600)
                return f"{hours}h ago"
            elif seconds < 604800:
                days = int(seconds // 86400)
                return f"{days}d ago"
            else:
                return updated.strftime("%b %d")
        except Exception:
            return ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "model": self.model,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "messages": [asdict(m) for m in self.messages],
        }

    @classmethod
    def from_dict(cls, data: dict) -> Session:
        msgs = [SessionMessage(**m) for m in data.get("messages", [])]
        return cls(
            id=data.get("id", ""),
            title=data.get("title", "New session"),
            model=data.get("model", ""),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            messages=msgs,
        )


class SessionManager:
    """Manages local session files in ~/.ovo/sessions/."""

    def __init__(self, sessions_dir: Optional[Path] = None):
        self.sessions_dir = sessions_dir or SESSIONS_DIR
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{session_id}.json"

    def create(self, model: str = "", title: str = "") -> Session:
        """Create a new session."""
        return Session(model=model, title=title or "New session")

    def save(self, session: Session) -> None:
        """Save a session to disk."""
        session.updated_at = datetime.now(timezone.utc).isoformat()
        path = self._path(session.id)
        with open(path, "w") as f:
            json.dump(session.to_dict(), f, indent=2)

    def load(self, session_id: str) -> Optional[Session]:
        """Load a session from disk."""
        path = self._path(session_id)
        if not path.exists():
            return None
        try:
            with open(path) as f:
                data = json.load(f)
            return Session.from_dict(data)
        except (json.JSONDecodeError, Exception):
            return None

    def list_sessions(self, limit: int = 20) -> List[Session]:
        """List all sessions, sorted by most recently updated."""
        sessions = []
        for path in self.sessions_dir.glob("*.json"):
            try:
                with open(path) as f:
                    data = json.load(f)
                sessions.append(Session.from_dict(data))
            except (json.JSONDecodeError, Exception):
                continue

        sessions.sort(key=lambda s: s.updated_at, reverse=True)
        return sessions[:limit]

    def delete(self, session_id: str) -> bool:
        """Delete a session file."""
        path = self._path(session_id)
        if path.exists():
            path.unlink()
            return True
        return False

    @staticmethod
    def auto_title(messages: List[SessionMessage]) -> str:
        """Generate a title from the first user message."""
        for msg in messages:
            if msg.role == "user":
                text = msg.content.strip().replace("\n", " ")
                if len(text) > 50:
                    return text[:47] + "…"
                return text
        return "New session"
