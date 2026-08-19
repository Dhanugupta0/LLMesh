"""Pydantic domain models for OVO."""

from enum import Enum
from pydantic import BaseModel, Field
from typing import List


class ModelStatus(Enum):
    """Status of a model on the server."""
    ACTIVE = "active"
    INACTIVE = "inactive"
    UNAVAILABLE = "unavailable"
    RATE_LIMITED = "rate_limited"

    @property
    def icon(self) -> str:
        return {
            ModelStatus.ACTIVE: "●",
            ModelStatus.INACTIVE: "○",
            ModelStatus.UNAVAILABLE: "✗",
            ModelStatus.RATE_LIMITED: "⚠",
        }[self]

    @property
    def label(self) -> str:
        return {
            ModelStatus.ACTIVE: "Active",
            ModelStatus.INACTIVE: "Inactive",
            ModelStatus.UNAVAILABLE: "Unavailable",
            ModelStatus.RATE_LIMITED: "Rate Limited",
        }[self]

    @property
    def selectable(self) -> bool:
        """Whether this model can be selected by the user."""
        return self == ModelStatus.ACTIVE


class ModelInfo(BaseModel):
    """Information about an available LLM model."""
    name: str = Field(description="Frontend display name")
    backend_name: str = Field(default="")
    provider: str = Field(default="")
    server_url: str = Field(default="")
    modes: List[str] = Field(default_factory=list)
    capabilities: List[str] = Field(default_factory=list)
    context_window: int = Field(default=0)
    tool_support: bool = Field(default=False)
    vision_support: bool = Field(default=False)
    reasoning_support: bool = Field(default=False)
    streaming_support: bool = Field(default=True)
    status: bool = Field(default=True)
    priority: int = Field(default=0)
    weight: float = Field(default=1.0)

    model_config = {"protected_namespaces": ()}

    @property
    def model_status(self) -> ModelStatus:
        """Derive ModelStatus from the boolean status field."""
        return ModelStatus.ACTIVE if self.status else ModelStatus.INACTIVE

    @property
    def display_name(self) -> str:
        """Short display name — strip provider prefix for readability."""
        name = self.name
        # e.g. "openai/gpt-oss-20b:free" → "gpt-oss-20b (free)"
        if "/" in name:
            name = name.split("/", 1)[1]
        if ":free" in name:
            name = name.replace(":free", " (free)")
        return name


class ProviderInfo(BaseModel):
    """Provider status."""
    name: str
    server_url: str = ""
    healthy: bool = True
    models: List[str] = Field(default_factory=list)


class UsageInfo(BaseModel):
    """Token usage for a request."""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    model: str = ""
    latency_ms: float = 0

    model_config = {"protected_namespaces": ()}
