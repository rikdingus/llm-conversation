"""Generic client for OpenAI-compatible chat/completions APIs."""

import json
from collections.abc import AsyncGenerator
from typing import Any

import aiohttp

from . import (
    DEFAULT_TIMEOUT,
    STREAM_TIMEOUT,
    ProviderClient,
    iter_sse_data,
    raise_for_status,
)


class OpenAICompatibleClient(ProviderClient):
    """Client for APIs implementing the OpenAI chat/completions spec."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        base_url: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        """Initialize the client."""
        super().__init__(session, api_key)
        self._base_url = base_url.rstrip("/")
        self._extra_headers = extra_headers or {}

    @property
    def _headers(self) -> dict[str, str]:
        """Return request headers."""
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            **self._extra_headers,
        }

    async def async_list_models(self) -> list[str]:
        """List available models (also used to validate the API key)."""
        async with self._session.get(
            f"{self._base_url}/models",
            headers=self._headers,
            timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
        ) as resp:
            await raise_for_status(resp)
            data = await resp.json()
            return [
                model["id"] for model in data.get("data", []) if model.get("id")
            ]

    def build_payload(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        """Build the chat/completions request payload."""
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload

    async def async_stream_chat(  # noqa: C901
        self, payload: dict[str, Any]
    ) -> AsyncGenerator[dict[str, Any]]:
        """Stream a chat completion, yielding canonical delta dicts."""
        async with self._session.post(
            f"{self._base_url}/chat/completions",
            headers=self._headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=STREAM_TIMEOUT),
        ) as resp:
            await raise_for_status(resp)
            role_sent = False
            async for data in iter_sse_data(resp):
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if usage := chunk.get("usage"):
                    # Mistral sends usage in the final chunk.
                    yield {
                        "usage": {
                            "input_tokens": usage.get("prompt_tokens", 0),
                            "output_tokens": usage.get("completion_tokens", 0),
                        }
                    }
                choices = chunk.get("choices") or [{}]
                choice = choices[0]
                delta = choice.get("delta") or {}
                content = delta.get("content")
                tool_calls = delta.get("tool_calls")
                if not role_sent and (content or tool_calls):
                    yield {"role": "assistant"}
                    role_sent = True
                if content:
                    yield {"content": content}
                if tool_calls:
                    yield {"tool_calls": tool_calls}
                finish_reason = choice.get("finish_reason")
                if finish_reason:
                    if finish_reason in ("tool_calls", "function_call"):
                        yield {"finish_reason": "tool_calls"}
                        yield {"tool_calls_done": True}
                    elif finish_reason == "length":
                        yield {"finish_reason": "length"}
                    else:
                        yield {"finish_reason": "stop"}
