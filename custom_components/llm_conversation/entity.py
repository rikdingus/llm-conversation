"""Base entity for the LLM Conversation agents."""

from __future__ import annotations

import base64
from collections.abc import AsyncGenerator, Callable, Iterable
import json
from mimetypes import guess_file_type
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
import probatio

from homeassistant.components import conversation
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, llm
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.json import json_dumps

from .const import (
    CONF_CHAT_MODEL,
    CONF_MAX_TOKENS,
    CONF_TEMPERATURE,
    DEFAULT_CHAT_MODELS,
    DOMAIN,
    LOGGER,
    MAX_TOOL_ITERATIONS,
    PROVIDER_LABELS,
    RECOMMENDED_MAX_TOKENS,
    RECOMMENDED_TEMPERATURE,
)
from .providers import (
    ProviderAuthError,
    ProviderConnectionError,
    ProviderError,
    ProviderRateLimitError,
)

if TYPE_CHECKING:
    from . import LLMConversationConfigEntry

# Unsupported OpenAI schema keys that are removed from tool parameters,
# mirroring the core conversation integrations.
_UNSUPPORTED_SCHEMA_KEYS = {"oneOf", "anyOf", "allOf", "enum", "not"}


def _format_tool(
    tool: llm.Tool, custom_serializer: Callable[[Any], Any] | None
) -> dict[str, Any]:
    """Format tool specification in the canonical (OpenAI) format."""
    schema = probatio.to_openapi(
        tool.parameters, custom_serializer=custom_serializer, openapi_version="3.1.0"
    )
    if _UNSUPPORTED_SCHEMA_KEYS.intersection(schema):
        schema = {k: v for k, v in schema.items() if k not in _UNSUPPORTED_SCHEMA_KEYS}
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": schema,
        },
    }


def _convert_content_to_param(
    chat_content: Iterable[conversation.Content],
) -> list[dict[str, Any]]:
    """Convert chat log content into canonical (OpenAI-style) messages."""
    messages: list[dict[str, Any]] = []

    for content in chat_content:
        if isinstance(content, conversation.ToolResultContent):
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": content.tool_call_id,
                    "content": json_dumps(
                        {"data": content.result.data, "error": content.result.error}
                    ),
                }
            )
            continue

        if isinstance(content, conversation.AssistantContent):
            message: dict[str, Any] = {"role": "assistant"}
            if content.content:
                message["content"] = content.content
            if content.tool_calls:
                message["tool_calls"] = [
                    {
                        "id": tool_call.id,
                        "type": "function",
                        "function": {
                            "name": tool_call.tool_name,
                            "arguments": json_dumps(tool_call.tool_args),
                        },
                    }
                    for tool_call in content.tool_calls
                ]
            if "content" in message or "tool_calls" in message:
                messages.append(message)
            continue

        if content.content:
            messages.append({"role": content.role, "content": content.content})

    return messages


async def async_prepare_images_for_prompt(
    hass: HomeAssistant, files: list[tuple[Path, str | None]]
) -> list[dict[str, Any]]:
    """Convert image files into canonical multimodal content parts."""

    def append_files_to_content() -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = []

        for file_path, mime_type in files:
            if not file_path.exists():
                raise HomeAssistantError(f"`{file_path}` does not exist")

            if mime_type is None:
                mime_type = guess_file_type(file_path)[0]

            if not mime_type or not mime_type.startswith("image/"):
                raise HomeAssistantError(
                    f"`{file_path}` is not an image file, only images are supported"
                )

            base64_file = base64.b64encode(file_path.read_bytes()).decode("utf-8")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime_type};base64,{base64_file}",
                    },
                }
            )

        return content

    return await hass.async_add_executor_job(append_files_to_content)


class LLMConversationBaseEntity(Entity):
    """Base entity for LLM conversation agents."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, entry: LLMConversationConfigEntry) -> None:
        """Initialize the entity."""
        self.entry = entry
        self._attr_unique_id = entry.entry_id
        provider_label = PROVIDER_LABELS[entry.data["provider"]]
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer=provider_label,
            model=entry.options.get(
                CONF_CHAT_MODEL,
                DEFAULT_CHAT_MODELS[entry.data["provider"]],
            ),
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    async def _async_handle_chat_log(  # noqa: C901
        self, chat_log: conversation.ChatLog
    ) -> None:
        """Generate an answer for the chat log."""
        entry = self.entry
        client = entry.runtime_data
        options = entry.options
        model = options.get(
            CONF_CHAT_MODEL, DEFAULT_CHAT_MODELS[entry.data["provider"]]
        )
        temperature = options.get(CONF_TEMPERATURE, RECOMMENDED_TEMPERATURE)
        max_tokens = options.get(CONF_MAX_TOKENS, RECOMMENDED_MAX_TOKENS)

        messages = _convert_content_to_param(chat_log.content)

        # Handle image attachments by adding them to the last user message.
        last_content = chat_log.content[-1]
        if (
            last_content.role == "user"
            and getattr(last_content, "attachments", None)
            and messages
            and isinstance(messages[-1].get("content"), str)
        ):
            files = await async_prepare_images_for_prompt(
                self.hass,
                [(a.path, a.mime_type) for a in last_content.attachments],
            )
            messages[-1]["content"] = [
                {"type": "text", "text": messages[-1]["content"]},
                *files,
            ]

        tools: list[dict[str, Any]] = []
        if chat_log.llm_api:
            tools = [
                _format_tool(tool, chat_log.llm_api.custom_serializer)
                for tool in chat_log.llm_api.tools
            ]

        # To prevent infinite loops, limit the number of tool call rounds.
        for _iteration in range(MAX_TOOL_ITERATIONS):
            try:
                payload = client.build_payload(
                    model=model,
                    messages=messages,
                    tools=tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                content_stream = chat_log.async_add_delta_content_stream(
                    self.entity_id,
                    self._transform_stream(
                        self._trace_usage(chat_log, client.async_stream_chat(payload))
                    ),
                )
                messages.extend(
                    _convert_content_to_param(
                        [content async for content in content_stream]
                    )
                )
            except ProviderAuthError as err:
                LOGGER.error("Authentication with the provider failed: %s", err)
                # Show the "re-authenticate" prompt in Settings.
                self.entry.async_start_reauth(self.hass)
                raise HomeAssistantError(
                    "Authentication with the provider failed"
                ) from err
            except ProviderRateLimitError as err:
                LOGGER.error("Rate limited by the provider: %s", err)
                raise HomeAssistantError("Rate limited by the provider") from err
            except (ProviderConnectionError, aiohttp.ClientError, TimeoutError) as err:
                LOGGER.error("Error connecting to the provider: %s", err)
                raise HomeAssistantError(
                    "Error connecting to the provider"
                ) from err
            except ProviderError as err:
                LOGGER.error("Error talking to the provider: %s", err)
                raise HomeAssistantError("Error talking to the provider") from err

            if not chat_log.unresponded_tool_results:
                break
            LOGGER.debug("Continuing chat after tool call round %s", _iteration + 1)

    @staticmethod
    async def _trace_usage(
        chat_log: conversation.ChatLog, stream: AsyncGenerator[dict[str, Any]]
    ) -> AsyncGenerator[dict[str, Any]]:
        """Record token usage in the conversation trace, pass chunks through."""
        async for chunk in stream:
            if usage := chunk.get("usage"):
                chat_log.async_trace({"stats": usage})
                continue
            yield chunk

    @staticmethod
    def _flush_tool_calls(
        tool_calls: dict[int, dict[str, Any]]
    ) -> list[llm.ToolInput]:
        """Convert accumulated tool call fragments into tool inputs."""
        tool_inputs: list[llm.ToolInput] = []
        for index in sorted(tool_calls):
            tool_call = tool_calls[index]
            name = tool_call.get("name")
            if not name:
                continue
            try:
                tool_args = json.loads(tool_call.get("arguments") or "{}")
            except json.JSONDecodeError:
                tool_args = {}
            tool_inputs.append(
                llm.ToolInput(
                    id=tool_call.get("id") or f"call_{index}",
                    tool_name=name,
                    tool_args=tool_args,
                )
            )
        return tool_inputs

    async def _transform_stream(  # noqa: C901
        self, stream: AsyncGenerator[dict[str, Any]]
    ) -> AsyncGenerator[
        conversation.AssistantContentDeltaDict | conversation.ToolResultContentDeltaDict
    ]:
        """Transform a provider delta stream into HA delta dicts."""
        role_sent = False
        tool_calls: dict[int, dict[str, Any]] = {}

        async for chunk in stream:
            pending: list[dict[str, Any]] = []
            if chunk.get("role") == "assistant" and not role_sent:
                pending.append({"role": "assistant"})
                role_sent = True
            if content := chunk.get("content"):
                if not role_sent:
                    pending.append({"role": "assistant"})
                    role_sent = True
                pending.append({"content": content})
            if fragments := chunk.get("tool_calls"):
                if not role_sent:
                    pending.append({"role": "assistant"})
                    role_sent = True
                for fragment in fragments:
                    index = fragment.get("index", 0)
                    tool_call = tool_calls.setdefault(
                        index, {"id": None, "name": None, "arguments": ""}
                    )
                    if fragment.get("id"):
                        tool_call["id"] = fragment["id"]
                    function = fragment.get("function") or {}
                    if function.get("name"):
                        tool_call["name"] = function["name"]
                    if function.get("arguments"):
                        tool_call["arguments"] += function["arguments"]
            if chunk.get("finish_reason") == "length":
                raise HomeAssistantError(
                    "Response incomplete: the maximum number of tokens was"
                    " reached. Increase 'Maximum tokens' in the options."
                )
            if chunk.get("tool_calls_done"):
                for tool_input in self._flush_tool_calls(tool_calls):
                    pending.append({"tool_calls": [tool_input]})
                tool_calls.clear()
            for item in pending:
                yield item

        # Flush any tool calls that were not signalled as done.
        for tool_input in self._flush_tool_calls(tool_calls):
            yield {"tool_calls": [tool_input]}
