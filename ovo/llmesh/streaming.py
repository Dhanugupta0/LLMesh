"""SSE stream parser for OpenAI-compatible streaming responses."""

import json
from typing import AsyncIterator

import httpx


async def parse_sse_stream(response: httpx.Response) -> AsyncIterator[str]:
    """Parse an SSE stream and yield content tokens.

    Handles the standard OpenAI streaming format:
        data: {"choices": [{"delta": {"content": "token"}}]}
        data: [DONE]
    """
    async for line in response.aiter_lines():
        if not line.startswith("data: "):
            continue

        payload = line[6:].strip()

        if payload == "[DONE]":
            break

        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue

        # Check for error responses
        if "error" in chunk:
            error_msg = chunk["error"].get("message", "Unknown error")
            raise StreamError(error_msg)

        choices = chunk.get("choices", [])
        if not choices:
            continue

        delta = choices[0].get("delta", {})
        content = delta.get("content", "")
        if content:
            yield content


class StreamError(Exception):
    """Raised when the SSE stream contains an error."""
    pass
