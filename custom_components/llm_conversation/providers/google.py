"""Client for the Google Gemini API."""

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

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiClient(ProviderClient):
    """Client for the Google Gemini API."""

    @property
    def _headers(self) -> dict[str, str]:
        """Return request headers."""
        return {
            "x-goog-api-key": self._api_key,
            "Content-Type": "application/json",
        }

    async def async_list_models(self) -> list[str]:
        """List models that support generateContent."""
        async with self._session.get(
            f"{GEMINI_BASE_URL}/models",
            headers=self._headers,
            timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
        ) as resp:
            await raise_for_status(resp)
            data = await resp.json()
            return [
                model["name"].removeprefix("models/")
                for model in data.get("models", [])
                if model.get("name")
                and "generateContent" in model.get("supportedGenerationMethods", [])
            ]

    def build_payload(  # noqa: C901
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        """Build the streamGenerateContent request payload."""
        system_texts: list[str] = []
        contents: list[dict[str, Any]] = []
        tool_id_to_name: dict[str, str] = {}

        for message in messages:
            role = message.get("role")
            content = message.get("content")

            if role == "system":
                system_texts.append(
                    content if isinstance(content, str) else json.dumps(content)
                )
                continue

            if role == "user":
                parts = self._convert_user_parts(content)
                self._append(contents, "user", parts)
                continue

            if role == "assistant":
                parts: list[dict[str, Any]] = []
                if content:
                    parts.append({"text": content})
                for tool_call in message.get("tool_calls") or []:
                    function = tool_call.get("function", {})
                    name = function.get("name")
                    tool_id_to_name[tool_call.get("id", "")] = name
                    try:
                        arguments = json.loads(function.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        arguments = {}
                    parts.append({"functionCall": {"name": name, "args": arguments}})
                if parts:
                    self._append(contents, "model", parts)
                continue

            if role == "tool":
                name = tool_id_to_name.get(message.get("tool_call_id", ""), "")
                try:
                    parsed = json.loads(content) if content else {}
                except (json.JSONDecodeError, TypeError):
                    parsed = {"output": content}
                if not isinstance(parsed, dict):
                    parsed = {"output": parsed}
                self._append(
                    contents,
                    "user",
                    [{"functionResponse": {"name": name, "response": {"result": parsed}}}],
                )

        payload: dict[str, Any] = {
            "model": model,
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_texts:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_texts)}]}
        if tools:
            payload["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": tool["function"]["name"],
                            "description": tool["function"]["description"],
                            "parameters": tool["function"]["parameters"],
                        }
                        for tool in tools
                    ]
                }
            ]
        return payload

    @staticmethod
    def _convert_user_parts(content: Any) -> list[dict[str, Any]]:
        """Convert canonical user content to Gemini parts."""
        if isinstance(content, str):
            return [{"text": content}]
        if not isinstance(content, list):
            return [{"text": str(content)}]
        parts: list[dict[str, Any]] = []
        for part in content:
            if part.get("type") == "text":
                parts.append({"text": part.get("text", "")})
            elif part.get("type") == "image_url":
                url = part.get("image_url", {}).get("url", "")
                if url.startswith("data:"):
                    header, _, data = url[len("data:") :].partition(",")
                    mime_type = header.split(";")[0] or "image/jpeg"
                    parts.append({"inlineData": {"mimeType": mime_type, "data": data}})
        return parts or [{"text": ""}]

    @staticmethod
    def _append(
        contents: list[dict[str, Any]], role: str, parts: list[dict[str, Any]]
    ) -> None:
        """Append parts, merging consecutive contents of the same role."""
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
            return
        contents.append({"role": role, "parts": parts})

    async def async_stream_chat(  # noqa: C901
        self, payload: dict[str, Any]
    ) -> AsyncGenerator[dict[str, Any]]:
        """Stream a completion, yielding canonical delta dicts."""
        body = {key: value for key, value in payload.items() if key != "model"}
        async with self._session.post(
            f"{GEMINI_BASE_URL}/models/{payload['model']}:streamGenerateContent",
            params={"alt": "sse"},
            headers=self._headers,
            json=body,
            timeout=aiohttp.ClientTimeout(total=STREAM_TIMEOUT),
        ) as resp:
            await raise_for_status(resp)
            role_sent = False
            tool_index = 0
            async for data in iter_sse_data(resp):
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    LOGGER.debug("Could not parse Gemini SSE chunk: %s", data)
                    continue
                for candidate in chunk.get("candidates", []):
                    parts = (candidate.get("content") or {}).get("parts") or []
                    has_function_calls = False
                    for part in parts:
                        if not role_sent and ("text" in part or "functionCall" in part):
                            yield {"role": "assistant"}
                            role_sent = True
                        if "text" in part:
                            yield {"content": part["text"]}
                        elif "functionCall" in part:
                            has_function_calls = True
                            function_call = part["functionCall"]
                            yield {
                                "tool_calls": [
                                    {
                                        "index": tool_index,
                                        "id": f"call_{tool_index}",
                                        "type": "function",
                                        "function": {
                                            "name": function_call.get("name"),
                                            "arguments": json.dumps(
                                                function_call.get("args") or {}
                                            ),
                                        },
                                    }
                                ]
                            }
                            tool_index += 1
                    finish_reason = candidate.get("finishReason")
                    if finish_reason:
                        if has_function_calls:
                            yield {"finish_reason": "tool_calls"}
                            yield {"tool_calls_done": True}
                        elif finish_reason == "MAX_TOKENS":
                            yield {"finish_reason": "length"}
                        else:
                            yield {"finish_reason": "stop"}
