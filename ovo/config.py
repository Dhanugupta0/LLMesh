"""OVO configuration management.

Stores config at ~/.ovo/config.json.
Supports environment variable overrides.
"""

import json
import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


# ── Paths ───────────────────────────────────────────────────────────────

OVO_DIR = Path.home() / ".ovo"
CONFIG_FILE = OVO_DIR / "config.json"
SESSIONS_DIR = OVO_DIR / "sessions"
LOGS_DIR = OVO_DIR / "logs"


class OvoConfig(BaseModel):
    """Application configuration."""

    # LLMesh connection
    api_url: str = Field(default="http://localhost:8087")
    api_key: str = Field(default="")

    # Defaults
    default_mode: str = Field(default="build")
    default_model: str = Field(default="")

    # Current state (persisted across restarts)
    current_mode: str = Field(default="build")
    current_model: str = Field(default="")
    current_provider: str = Field(default="")

    # Behaviour
    stream: bool = Field(default=True)
    auto_fallback: bool = Field(default=True)
    auto_compact: bool = Field(default=True)
    show_usage: bool = Field(default=True)

    # Context budget
    system_budget: int = Field(default=10_000)
    output_budget: int = Field(default=10_000)

    # Tool approval
    tool_approval_policy: str = Field(default="ask")  # ask | allow_safe | allow_all

    model_config = {"protected_namespaces": ()}

    @classmethod
    def load(cls) -> "OvoConfig":
        """Load config from disk, with env var overrides."""
        config = cls()

        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE) as f:
                    data = json.load(f)
                config = cls(**data)
            except (json.JSONDecodeError, Exception):
                pass

        # Environment variable overrides
        if url := os.environ.get("OVO_API_URL") or os.environ.get("LLMESH_API_URL"):
            config.api_url = url
        if key := os.environ.get("OVO_API_KEY") or os.environ.get("LLMESH_API_KEY"):
            config.api_key = key

        return config

    def save(self):
        """Persist config to disk."""
        OVO_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(self.model_dump(), f, indent=2)

    def is_configured(self) -> bool:
        """Whether OVO has a stored API key."""
        return bool(self.api_key)
