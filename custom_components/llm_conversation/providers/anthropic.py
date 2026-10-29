"""Client for the Anthropic Claude Messages API."""

import json
from collections.abc import AsyncGenerator
from typing import Any

import aiohttp

from ..const import LOGGER
from . import (
    DEFAULT_TIMEOUT,
    STREAM_TIMEOUT,
    ProviderClient,
    iter_sse_data,
    raise_for_status,
)

ANTHROPIC_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2026-10-21"


def _parse_data_uri(url: str) -> tuple[str, str] | None:
    """Parse a data URI into (mime_type, base64 data)."""
    prefix = "data:"
    if not url.startswith(prefix):
        return None
    header, _, data = url[len(prefix) :].partition(",")
    mime_type = header.split(";")[0] or "image/jpeg"
    return mime_type, data


class AnthropicClient(ProviderClient):
    """Client for the Anthropic Messages API."""

    @property
    def _headers(self) -> dict[str, str]:
        """Return request headers."""
        return {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }

    async def async_list_models(self) -> list[str]:
        """List available models (also used to validate the API key)."""
        async with self._session.get(
            f"{ANTHROPIC_BASE_URL}/v1/models",
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
        """Build the Messages API request payload.

        Current Claude models reject non-default sampling parameters,
        so no temperature is sent.
        """
        system_texts, converted = self._convert_messages(messages)
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": converted,
            "stream": True,
        }
        # Prompt caching: tools and the system prompt (Home Assistant
        # instructions + exposed entities) are identical on every tool-call
        # round, so mark them cacheable. Cache reads cost a fraction of
        # normal input tokens.
        if tools:
            payload["tools"] = [
                {
                    "name": tool["function"]["name"],
                    "description": tool["function"]["description"],
                    "input_schema": tool["function"]["parameters"],
                }
                for tool in tools
            ]
            payload["tools"][-1]["cache_control"] = {"type": "ephemeral"}
        if system_texts:
            payload["system"] = [
                {
                    "type": "text",
                    "text": "\n\n".join(system_texts),
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        return payload

    @classmethod
    def _convert_messages(
        cls, messages: list[dict[str, Any]]
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """Convert canonical messages to the Anthropic format."""
        system_texts: list[str] = []
        converted: list[dict[str, Any]] = []

        for message in messages:
            role = message.get("role")
            content = message.get("content")

            if role == "system":
                system_texts.append(
                    content if isinstance(content, str) else json.dumps(content)
                )
                continue

            if role == "user":
                blocks = cls._convert_user_content(content)
                cls._append(converted, "user", blocks)
                continue

            if role == "assistant":
                blocks: list[dict[str, Any]] = []
                if content:
                    blocks.append({"type": "text", "text": content})
                for tool_call in message.get("tool_calls") or []:
                    function = tool_call.get("function", {})
                    try:
                        arguments = json.loads(function.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        arguments = {}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tool_call.get("id"),
                            "name": function.get("name"),
                            "input": arguments,
                        }
                    )
                if blocks:
                    cls._append(converted, "assistant", blocks)
                continue

            if role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": message.get("tool_call_id"),
                    "content": content or "",
                }
                cls._append(converted, "user", [block])

        return system_texts, converted

    @staticmethod
    def _convert_user_content(content: Any) -> list[dict[str, Any]]:
        """Convert canonical user content to Anthropic content blocks."""
        if isinstance(content, str):
            return [{"type": "text", "text": content}]
        if not isinstance(content, list):
            return [{"type": "text", "text": str(content)}]
        blocks: list[dict[str, Any]] = []
        for part in content:
            if part.get("type") == "text":
                blocks.append({"type": "text", "text": part.get("text", "")})
            elif part.get("type") == "image_url":
                parsed = _parse_data_uri(
                    part.get("image_url", {}).get("url", "")
                )
                if parsed:
                    mime_type, data = parsed
                    blocks.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": data,
                            },
                        }
                    )
        return blocks or [{"type": "text", "text": ""}]

    @staticmethod
    def _append(
        converted: list[dict[str, Any]], role: str, blocks: list[dict[str, Any]]
    ) -> None:
        """Append blocks, merging consecutive messages of the same role.

        The Anthropic API requires messages to alternate between
        user and assistant roles.
        """
        if converted and converted[-1]["role"] == role:
            converted[-1]["content"].extend(blocks)
            return
        converted.append({"role": role, "content": blocks})

    async def async_stream_chat(  # noqa: C901
        self, payload: dict[str, Any]
    ) -> AsyncGenerator[dict[str, Any]]:
        """Stream a Messages API completion, yielding canonical deltas."""
        async with self._session.post(
            f"{ANTHROPIC_BASE_URL}/v1/messages",
            headers=self._headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=STREAM_TIMEOUT),
        ) as resp:
            await raise_for_status(resp)
            role_sent = False
            input_tokens = 0
            async for data in iter_sse_data(resp):
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    LOGGER.debug("Could not parse Anthropic SSE chunk: %s", data)
                    continue
                event_type = event.get("type")
                if event_type == "message_start":
                    usage = (event.get("message") or {}).get("usage") or {}
                    input_tokens = (
                        usage.get("input_tokens", 0)
                        + usage.get("cache_read_input_tokens", 0)
                        + usage.get("cache_creation_input_tokens", 0)
                    )
                    yield {"role": "assistant"}
                    role_sent = True
                elif event_type == "content_block_start":
                    block = event.get("content_block", {})
                    if block.get("type") == "tool_use":
                        if not role_sent:
                            yield {"role": "assistant"}
                            role_sent = True
                        yield {
                            "tool_calls": [
                                {
                                    "index": event.get("index", 0),
                                    "id": block.get("id"),
                                    "type": "function",
                                    "function": {
                                        "name": block.get("name"),
                                        "arguments": "",
                                    },
                                }
                            ]
                        }
                elif event_type == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        if not role_sent:
                            yield {"role": "assistant"}
                            role_sent = True
                        yield {"content": delta.get("text", "")}
                    elif delta.get("type") == "input_json_delta":
                        yield {
                            "tool_calls": [
                                {
                                    "index": event.get("index", 0),
                                    "function": {
                                        "arguments": delta.get("partial_json", "")
                                    },
                                }
                            ]
                        }
                elif event_type == "message_delta":
                    if usage := event.get("usage"):
                        yield {
                            "usage": {
                                "input_tokens": input_tokens,
                                "output_tokens": usage.get("output_tokens", 0),
                            }
                        }
                    stop_reason = event.get("delta", {}).get("stop_reason")
                    if stop_reason == "tool_use":
                        yield {"finish_reason": "tool_calls"}
                        yield {"tool_calls_done": True}
                    elif stop_reason == "max_tokens":
                        yield {"finish_reason": "length"}
                    else:
                        yield {"finish_reason": "stop"}
