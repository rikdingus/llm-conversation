"""Base classes and helpers for the LLM Conversation providers.

Every provider translates the canonical (OpenAI-style) message and tool
format used by the entity layer into its own API format, and streams back
canonical delta dicts:

    {"role": "assistant"}                    # start of an assistant message
    {"content": "text fragment"}
    {"tool_calls": [<fragment>]}             # accumulating tool call fragments
    {"tool_calls_done": True}                # all tool calls are complete
    {"finish_reason": "stop"|"tool_calls"|"length"}

Tool call fragments follow the OpenAI streaming format:
    {"index": 0, "id": "call_x", "type": "function",
     "function": {"name": "x", "arguments": "<json fragment>"}}
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import Any

import aiohttp

from ..const import LOGGER

DEFAULT_TIMEOUT = 10
STREAM_TIMEOUT = 180
TRANSCRIBE_TIMEOUT = 120


class ProviderError(Exception):
    """Base class for provider errors."""


class ProviderAuthError(ProviderError):
    """Authentication with the provider failed."""


class ProviderRateLimitError(ProviderError):
    """The provider rate limited or rejected the request."""


class ProviderConnectionError(ProviderError):
    """Could not connect to the provider."""


async def raise_for_status(resp: aiohttp.ClientResponse) -> None:
    """Translate HTTP errors into provider exceptions."""
    if resp.status in (401, 403):
        raise ProviderAuthError(f"HTTP {resp.status}")
    if resp.status == 429:
        raise ProviderRateLimitError("HTTP 429")
    if resp.status >= 400:
        body = await resp.text()
        LOGGER.error("Provider API error %s: %s", resp.status, body[:500])
        raise ProviderError(f"HTTP {resp.status}")


async def iter_sse_data(resp: aiohttp.ClientResponse) -> AsyncGenerator[str]:
    """Yield the data payloads of a server-sent events stream."""
    async for raw_line in resp.content:
        line = raw_line.decode("utf-8").strip()
        if line.startswith("data:"):
            yield line.removeprefix("data:").strip()


class ProviderClient(ABC):
    """Base class for provider API clients."""

    def __init__(self, session: aiohttp.ClientSession, api_key: str) -> None:
        """Initialize the client."""
        self._session = session
        self._api_key = api_key

    @abstractmethod
    async def async_list_models(self) -> list[str]:
        """List available models (also used to validate the API key)."""

    @abstractmethod
    def build_payload(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        """Build the provider-specific request payload."""

    @abstractmethod
    def async_stream_chat(
        self, payload: dict[str, Any]
    ) -> AsyncGenerator[dict[str, Any]]:
        """Stream a chat completion, yielding canonical delta dicts."""
