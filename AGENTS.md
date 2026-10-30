# AGENTS.md

Guidance for AI coding agents (and human contributors) working on this
repository. Read this before making changes.

## What this is

A custom Home Assistant integration, **LLM Conversation** (domain
`llm_conversation`), that exposes four AI providers as Assist conversation
agents with real tool calling through the Home Assistant LLM API:

- Mistral AI (`api.mistral.ai`)
- Anthropic Claude (`api.anthropic.com`)
- Google Gemini (`generativelanguage.googleapis.com`)
- GitHub Models / Copilot (`models.github.ai`, OpenAI-compatible)

Key constraints:

- **Targets Home Assistant 2026.10+** (verified against 2026.10.4). Follow
  the entity pattern of the core `openai_conversation` and `ollama`
  integrations of that tag: `_async_handle_message(user_input, chat_log)`,
  `chat_log.async_provide_llm_data(...)`, `_async_handle_chat_log(chat_log)`,
  `conversation.async_set_agent(hass, entry, self)`.
- **Zero external pip dependencies.** Only `aiohttp` (ships with HA) plus
  the standard library. Never add anything to `manifest.json` `requirements`.
- **`probatio`, not voluptuous.** Home Assistant replaced voluptuous;
  config schemas and tool parameter serialization use
  `probatio.Schema` and `probatio.to_openapi(..., openapi_version="3.1.0")`.
- **Streaming is always on** (`"stream": True`). The `ChatLog` decides
  whether the response is delivered streamed or buffered.
- **Translations**: user-visible strings live in `strings.json` +
  `translations/en.json` (identical content) and `translations/nl.json`.

## Repository layout

```
llm-conversation/
├── AGENTS.md                     ← you are here
├── LICENSE                       ← MIT
├── README.md                     ← user-facing install & usage docs
├── hacs.json                     ← HACS metadata
├── test_smoke.py                  ← offline test suite (stubs HA/probatio/aiohttp)
├── test_setup_mock.py            ← setup script vs. mock HA (REST + websocket)
├── CHANGELOG.md                  ← newest version first
├── scripts/test_realtime.py      ← live API test (Mistral + HA REST, stdlib only)
├── scripts/setup_assist.py       ← post-HACS setup via the HA API (stdlib only)
├── .github/workflows/validate.yaml
└── custom_components/llm_conversation/
    ├── manifest.json
    ├── const.py                  ← domain, provider registry, model lists
    ├── __init__.py               ← setup/unload, client factory
    ├── config_flow.py            ← user/reauth steps + options flow
    ├── conversation.py           ← ConversationEntity, registration with Assist
    ├── entity.py                 ← chat-log → provider loop, stream transform
    ├── stt.py                     ← Voxtral SpeechToTextEntity (Mistral only)
    ├── tts.py                     ← Voxtral TextToSpeechEntity (Mistral only)
    ├── brand/icon.png, icon@2x.png ← brand assets (required by HACS validation)
    └── providers/
        ├── __init__.py           ← ProviderClient ABC, error classes, SSE helpers
        ├── openai_compat.py      ← generic chat/completions client + SSE parser
        ├── mistral.py            ← MistralClient (OpenAI-compatible)
        ├── github_models.py     ← GitHubModelsClient (OpenAI-compatible)
        ├── anthropic.py          ← Claude Messages API adapter
        └── google.py             ← Gemini API adapter
```

## Architecture: the canonical format

The entity layer speaks ONE internal format (OpenAI-style) and every
provider adapter translates to/from its own wire format:

**Messages**

```python
{"role": "system", "content": "instructions"}
{"role": "user", "content": "text" | [{"type": "text", "text": ...},
                                       {"type": "image_url", "image_url": {"url": "data:<mime>;base64,..."}}]}
{"role": "assistant", "content": "text",           # content optional
 "tool_calls": [{"id": "call_x", "type": "function",
                 "function": {"name": "HassTurnOn", "arguments": "<json str>"}}]}
{"role": "tool", "tool_call_id": "call_x", "content": "<json str>"}
```

**Tools**

```python
{"type": "function",
 "function": {"name": ..., "description": ..., "parameters": <openapi 3.1 schema>}}
```

**Provider delta stream** (what `async_stream_chat` yields, and the ONLY
thing `entity._transform_stream` understands):

```python
{"role": "assistant"}          # emitted once, before the first content/tool delta
{"content": "fragment"}
{"tool_calls": [ <fragment> ]}   # OpenAI-style fragments, accumulated by index:
                                # {"index": 0, "id": ..., "type": "function",
                                #  "function": {"name": ..., "arguments": "<json fragment>"}}
{"tool_calls_done": True}        # optional early-completion signal
{"finish_reason": "stop" | "tool_calls" | "length"}
{"usage": {"input_tokens": int, "output_tokens": int}}   # optional, any time
```

`usage` chunks are consumed by `entity._trace_usage` (written to the
conversation trace as `{"stats": ...}`) and never reach
`_transform_stream`. `finish_reason == "length"` makes `_transform_stream`
raise a `HomeAssistantError` so a truncated answer is never shown as if it
were complete.

`entity._transform_stream` accumulates the fragments and, when a tool call
is complete (done signal or stream end), yields HA delta dicts:
`{"role": "assistant"}`, `{"content": ...}`,
`{"tool_calls": [llm.ToolInput(id, tool_name, tool_args)]}`. The `ChatLog`
then executes the tool itself and appends `ToolResultContent`, which the
next loop iteration converts back to canonical `role: "tool"` messages.

**The tool loop** (`entity._async_handle_chat_log`, mirrors core):

```python
for _iteration in range(MAX_TOOL_ITERATIONS):
    payload = client.build_payload(...)
    content_stream = chat_log.async_add_delta_content_stream(
        self.entity_id,
        self._transform_stream(
            self._trace_usage(chat_log, client.async_stream_chat(payload))
        ),
    )
    messages.extend(_convert_content_to_param([c async for c in content_stream]))
    if not chat_log.unresponded_tool_results:
        break
```

**Error model**: providers raise `ProviderAuthError` / `ProviderRateLimitError`
/ `ProviderConnectionError` / `ProviderError`; the entity translates them to
`HomeAssistantError` (and starts reauth on `ProviderAuthError`); setup
translates them to `ConfigEntryAuthFailed` (reauth flow) /
`ConfigEntryNotReady` (automatic retry). Raw `aiohttp.ClientError` and
`TimeoutError` are treated as connection errors everywhere (setup, config
flow, conversation): Home Assistant on the router often starts before the
internet is up, and setup must retry instead of failing permanently.

## Provider-specific gotchas

- **Anthropic**: do NOT send `temperature` — API version `2026-10-21`
  removed `temperature`/`top_p`/`top_k` (400 on every model; the options
  flow hides the field for Anthropic). Prompt caching: the last tool and
  the system block carry `cache_control: {"type": "ephemeral"}` (the system
  prompt is sent as a block list for this); both repeat on every tool-call
  round. Messages must strictly alternate user/assistant — the
  adapter merges consecutive same-role turns. Tool results are user turns
  with `tool_result` blocks; tool calls are `tool_use` blocks.
- **Gemini**: no tool call IDs exist; the adapter synthesizes random
  `call_<12 hex>` ids (unique across tool-call rounds — per-round counters
  collided) and maps them back to function names via the preceding
  assistant turn. Tool results are `functionResponse` parts in a
  `user` turn. System prompt goes to `systemInstruction`.
- **GitHub Models**: catalog lives at `/catalog/models`, inference at
  `/inference/chat/completions` (OpenAI schema). Auth: PAT with
  `models: read`. Model IDs are namespaced (`openai/gpt-4.1`).
- All model dropdowns must keep `custom_value=True` — static model lists
  go stale fast; the options flow fetches the live catalog as primary
  source.

## Speech: Voxtral STT/TTS (Mistral entries only)

`stt.py` and `tts.py` load only when the entry's provider is
`PROVIDER_MISTRAL` — `__init__.py` appends `[Platform.STT, Platform.TTS]`
to the forwarded platforms for those entries (setup AND unload).

- **STT** (`/v1/audio/transcriptions`): the Assist pipeline delivers raw
  PCM chunks; `stt.py` wraps them in a WAV container (header built with
  `struct`, `<4sI4s4sIHHIIHH4sI`) and posts them as multipart
  (`aiohttp.FormData`). Use the `_auth_headers` property for multipart
  requests — the default `_headers` sets `Content-Type: application/json`,
  which would break the multipart boundary.
- `check_metadata` is overridden to always return `True`: Voxtral handles
  many languages and audio formats, and a mismatch should degrade quality,
  never block a pipeline. (`assist_pipeline/default_pipeline.py` calls
  `stt_provider.check_metadata(metadata)` and aborts on `False`.)
- **`SpeechResult(text=..., result=...)`** — the dataclass field for the
  outcome is named `result`, NOT `state` (verified in 2026.10.4
  `stt/models.py`; the pipeline checks
  `result.result != stt.SpeechResultState.SUCCESS`).
- **TTS** (`/v1/audio/speech`): JSON request, response is
  `{"audio_data": "<base64>"}` which the client decodes to bytes. Output
  format is fixed to `mp3`. A **voice ID from Mistral Studio is required**
  by the API — it is stored per entry in options (`CONF_TTS_VOICE_ID`),
  not per service call. Content moderation can reject requests with 403.
- The STT/TTS models are options-flow fields (dropdowns with
  `custom_value=True`), NOT config-entry data — changing a model must not
  require re-auth. Defaults live in `const.py`
  (`DEFAULT_STT_MODEL`, `DEFAULT_TTS_MODEL`).
- Both entities attach to the same device as the conversation agent
  (`identifiers={(DOMAIN, entry.entry_id)}`), unique IDs
  `f"{entry.entry_id}-stt"` / `-tts`.

## Adding a new provider: checklist

1. `const.py`: new `PROVIDER_X` constant, entry in `PROVIDER_LABELS`,
   `CHAT_MODELS`, `DEFAULT_CHAT_MODELS`, and (if it accepts sampling
   params) `PROVIDERS_SUPPORTING_TEMPERATURE`.
2. `providers/x.py`: subclass `ProviderClient` (or `OpenAICompatibleClient`
   if the API is OpenAI-compatible) and implement `async_list_models`,
   `build_payload`, `async_stream_chat` per the contract above.
3. `__init__.py`: register the client class in `CLIENT_CLASSES` (the
   config flow imports the same map — there is no second registry).
4. Yield `{"usage": ...}` from the stream if the API reports token usage.
5. Extend `test_smoke.py` with a converter test and an SSE-stream test for
   the new provider (see the existing Anthropic/Gemini/OpenAI tests).
6. No new translation keys are needed unless you add new form fields.

## Testing

From the repository root (no Home Assistant installation required):

```bash
python3 -m py_compile $(find custom_components scripts -name "*.py")
python3 test_smoke.py       # must print: ALL SMOKE TESTS PASSED
python3 test_setup_mock.py  # must print: ALL SETUP-SCRIPT TESTS PASSED
```

`test_smoke.py` stubs `homeassistant`, `probatio` and `aiohttp` modules,
then exercises: chat-log → canonical conversion, Anthropic/Gemini message
conversion, full SSE → `ToolInput` pipelines for all three wire formats,
the Voxtral STT/TTS entities, token usage tracing, max-tokens truncation,
Anthropic cache markers, unique Gemini ids and empty transcriptions. It
locates the component relative to its own path, so it runs anywhere
(locally and in CI). Run it after every change to `entity.py` or `providers/`.
CI (`.github/workflows/validate.yaml`) runs HACS validation, hassfest and
both offline test files.

**`scripts/setup_assist.py`** drives a live Home Assistant through its
REST + websocket API. `test_setup_mock.py` mirrors these responses; a mock
that agrees with a wrong assumption hides the bug, so when you change the
script, check the facts below against the core source of the targeted tag
(all verified against 2026.10.4) and keep the mock in sync:

- `GET /api/` answers JSON `{"message": "API running."}`, not plain text.
- `GET /api/config/config_entries/entry?domain=…` returns
  `entry.as_json_fragment`: entry_id, title, state, … but **never
  `data`**, so the provider can't be read from it. The script recognises
  a Mistral entry by its STT entity (`config/entity_registry/list`, whose
  items carry `config_entry_id`, `platform`, `entity_id`, `disabled_by`).
- When a flow step fails, HA re-sends the form with the submitted values
  as `description.suggested_value` (`add_suggested_values_to_schema`), so
  printing a flow response can leak the API key: print only `errors`.
  The options form uses the same mechanism to show the current options,
  which the script reads to merge (submitting the form REPLACES options).
- Flows are aborted with `DELETE /api/config/config_entries/[options/]flow/{id}`.
- Websocket: path `/api/websocket` (mind double slashes), and
  `auth_required` may arrive in the same packet as the `101` handshake.
- `assist_pipeline/pipeline/list` → `{"pipelines": [...],
  "preferred_pipeline": id}`. `create` and `update` require **every**
  field of `PIPELINE_FIELDS` including `name` (`update` replaces the
  pipeline, so carry over wake word and `prefer_local_intents`);
  `update` and `set_preferred` identify the pipeline with `pipeline_id`.
- LLM agents use `conversation_language: "*"`; the pipeline then uses the
  STT/TTS language for local intent fallback (`default_pipeline.py`).

**`scripts/test_realtime.py`** runs against the LIVE Mistral API and,
optionally, a running Home Assistant. Key facts (verified against the
2026.10.4 core source — keep them when editing):

- HA exposes the STT entity at `POST /api/stt/{entity_id}` with metadata
  in the `X-Speech-Content` header
  (`format=wav; codec=pcm; bit_rate=16; sample_rate=16000; channel=1; language=…`)
  and RAW PCM bytes as the body — the entity (not the caller) builds the
  WAV container. The script resamples the Mistral TTS WAV to 16 kHz mono
  PCM in pure Python for this.
- The TTS entity is reachable via `POST /api/tts_get_url`
  (`{"engine_id", "message", "language"}`) → `{"url"}` → fetch the
  unauthenticated proxy URL for the MP3.
- `POST /api/conversation/process` with `{"text", "agent_id"}` tests the
  agent. The response carries `response.speech.plain.speech`.
- The STT REST view checks `check_metadata` from the header metadata and
  validates against the enums, so the script must send values that exist
  (`wav`, `pcm`, `16`, `16000`, `1`).

## Style

- Python 3.13+ typing style, `from __future__ import annotations` in every
  module, single quotes are NOT mandated — follow the existing files.
- Use `LOGGER` from `const.py`; never log API keys or full request bodies.
- Keep functions under ~50 lines where practical; existing `# noqa: C901`
  markers flag the two intentionally complex stream parsers.
