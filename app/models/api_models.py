from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Dict, List, Optional, Any, Union
from datetime import datetime
from collections import defaultdict


class ModelUsage(BaseModel):
    """Model usage detail model"""

    requests: int = Field(default=0, description="Number of requests")
    tokens: float = Field(default=0, description="Token usage")


class ApiKeyUsage(BaseModel):
    """API key usage model"""

    usage: float = Field(default=0, description="Current usage")
    limit: float = Field(description="Usage limit")
    reqs: int = Field(default=0, description="Number of chat requests")
    created_at: Optional[str] = Field(default=None, description="Creation time")
    last_used: Optional[str] = Field(default=None, description="Last used time")
    phone: Optional[str] = Field(default=None, description="Phone number")
    model_usage: Dict[str, ModelUsage] = Field(
        default_factory=dict, description="Usage details per model"
    )

    model_config = {"protected_namespaces": ()}


class LLMServer(BaseModel):
    """LLM server configuration model"""

    url: str = Field(description="Server URL")
    model: Union[Dict[str, str], str, List[str]] = Field(
        description="Supported models. Can be a dict (key = client-facing model name, value = actual forwarded model name), a string, or a list"
    )
    apikey: Optional[str] = Field(default=None, description="API key")


class AppState(BaseModel):
    """Application state model"""

    llm_servers: Dict[str, Dict] = Field(
        default_factory=dict, description="LLM server configuration"
    )
    cloud_models: Dict[str, Dict[str, str]] = Field(
        default_factory=lambda: defaultdict(dict),
        description="Model API key mapping: {model: {server_url: apikey}}"
    )
    model_mapping: Dict[str, List] = Field(
        default_factory=lambda: defaultdict(list), description="Model to server mapping"
    )
    model_name_mapping: Dict[str, Dict[str, Any]] = Field(
        default_factory=lambda: defaultdict(dict),
        description="Nested mapping of client model name to (server_url -> actual model info)"
    )
    api_usage: Dict[str, ApiKeyUsage] = Field(
        default_factory=dict, description="API usage"
    )

    model_config = {"protected_namespaces": ()}


class UsageStats(BaseModel):
    """Usage statistics model"""

    less_than_100: int = Field(default=0, description="Count of keys with usage below 100")
    between_100_and_10000: int = Field(
        default=0, description="Count of keys with usage between 100 and 10000"
    )
    more_than_10000: int = Field(default=0, description="Count of keys with usage above 10000")
    total_usage: float = Field(default=0, description="Total usage")
    total_entries: int = Field(default=0, description="Total number of entries")
    total_reqs: int = Field(default=0, description="Total number of requests")
    current_time: str = Field(description="Current time")
    api_keys: List[Dict] = Field(default_factory=list, description="API key usage details")