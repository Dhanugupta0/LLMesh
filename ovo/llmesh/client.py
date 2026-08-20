"""LLMesh API client for OVO.

Talks only to the LLMesh gateway. Never contacts upstream providers directly.
All provider authentication is handled server-side.
"""

import json
import time
from typing import Optional, Dict, List, Any, AsyncIterator

import httpx

from ovo.llmesh import ModelInfo, ProviderInfo, UsageInfo
from ovo.llmesh.streaming import parse_sse_stream, StreamError


class APIError(Exception):
    """Raised when the LLMesh API returns an error."""
    def __init__(self, message: str, status_code: int = 0):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class LLMeshClient:
    """Async HTTP client for the LLMesh API."""

    def __init__(self, base_url: str = "http://localhost:8087", api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._client: Optional[httpx.AsyncClient] = None

    def _get_headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=10.0),
                headers=self._get_headers(),
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ── Health ──────────────────────────────────────────────────────

    async def verify_connection(self) -> bool:
        """Verify the LLMesh gateway is reachable and the API key is valid."""
        client = await self._ensure_client()
        try:
            resp = await client.get(f"{self.base_url}/v1/models", timeout=5.0)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def health_check(self) -> bool:
        """Quick health check."""
        return await self.verify_connection()

    # ── Models ──────────────────────────────────────────────────────

    async def list_models(self) -> List[ModelInfo]:
        """Fetch models from LLMesh. Uses the /v1/models endpoint."""
        client = await self._ensure_client()

        try:
            resp = await client.get(f"{self.base_url}/v1/models")
            if resp.status_code == 200:
                data = resp.json()
                models = []
                for m in data.get("data", []):
                    model_id = m.get("id", "")
                    provider = m.get("owned_by", "")
                    models.append(ModelInfo(
                        name=model_id,
                        backend_name=model_id,
                        provider=provider,
                        status=True,  # Server only returns active models
                    ))
                return models
        except (httpx.HTTPError, json.JSONDecodeError):
            pass

        return []

    async def list_providers(self) -> List[ProviderInfo]:
        """Fetch provider info."""
        client = await self._ensure_client()
        try:
            resp = await client.get(f"{self.base_url}/api/cli/providers")
            if resp.status_code == 200:
                data = resp.json()
                return [ProviderInfo(**p) for p in data.get("providers", [])]
        except (httpx.HTTPError, json.JSONDecodeError):
            pass
        return []

    # ── Chat ────────────────────────────────────────────────────────

    async def chat_stream(
        self,
        messages: List[Dict[str, str]],
        model: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        """Send a streaming chat completion request. Yields content tokens."""
        client = await self._ensure_client()
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens

        async with client.stream(
            "POST",
            f"{self.base_url}/v1/chat/completions",
            json=payload,
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=10.0),
        ) as response:
            if response.status_code != 200:
                body = await response.aread()
                raise APIError(
                    f"Server returned {response.status_code}: {body.decode()}",
                    response.status_code,
                )
            async for token in parse_sse_stream(response):
                yield token

    async def chat(
        self,
        messages: List[Dict[str, str]],
        model: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Send a non-streaming chat completion. Returns full response."""
        client = await self._ensure_client()
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens

        start = time.time()
        resp = await client.post(f"{self.base_url}/v1/chat/completions", json=payload)

        if resp.status_code != 200:
            raise APIError(f"Server returned {resp.status_code}: {resp.text}", resp.status_code)

        data = resp.json()
        latency_ms = (time.time() - start) * 1000
        usage = data.get("usage", {})

        content = ""
        choices = data.get("choices", [])
        if choices:
            content = choices[0].get("message", {}).get("content", "")

        return {
            "content": content,
            "usage": UsageInfo(
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
                model=model,
                latency_ms=latency_ms,
            ),
        }
