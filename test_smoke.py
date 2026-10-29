"""Smoke test: provider converters + entity stream transform (HA stubbed)."""
import asyncio
import importlib
import json
import struct
import sys
import types

# ---- Stub homeassistant + probatio + aiohttp ----
ha = types.ModuleType("homeassistant")
ha_components = types.ModuleType("homeassistant.components")
ha_components_conversation = types.ModuleType("homeassistant.components.conversation")
ha_core = types.ModuleType("homeassistant.core")
ha_exceptions = types.ModuleType("homeassistant.exceptions")
ha_helpers = types.ModuleType("homeassistant.helpers")
ha_dr = types.ModuleType("homeassistant.helpers.device_registry")
ha_llm = types.ModuleType("homeassistant.helpers.llm")
ha_entity = types.ModuleType("homeassistant.helpers.entity")
ha_json = types.ModuleType("homeassistant.helpers.json")


class Content:
    def __init__(self, role, content="", tool_calls=None):
        self.role = role
        self.content = content
        self.tool_calls = tool_calls


class AssistantContent(Content):
    pass


class ToolResultContent(Content):
    def __init__(self, tool_call_id, tool_name, result):
        super().__init__("tool")
        self.tool_call_id = tool_call_id
        self.tool_name = tool_name
        self.result = result


class HomeAssistantError(Exception):
    pass


class ToolInput:
    def __init__(self, id, tool_name, tool_args):
        self.id = id
        self.tool_name = tool_name
        self.tool_args = tool_args


ha_components_conversation.Content = Content
ha_components_conversation.AssistantContent = AssistantContent
ha_components_conversation.ToolResultContent = ToolResultContent
ha_components_conversation.AssistantContentDeltaDict = dict
ha_components_conversation.ToolResultContentDeltaDict = dict
ha_components_conversation.ChatLog = object
ha_core.HomeAssistant = object
ha_exceptions.HomeAssistantError = HomeAssistantError
ha_dr.DeviceInfo = dict
ha_dr.DeviceEntryType = type("DeviceEntryType", (), {"SERVICE": "service"})
ha_llm.ToolInput = ToolInput
ha_llm.Tool = object
ha_llm.DEFAULT_INSTRUCTIONS_PROMPT = "You are a home assistant."
ha_entity.Entity = object
ha_json.json_dumps = lambda obj, **kw: json.dumps(obj, default=str)


# ---- STT / TTS stubs (for the Voxtral speech entities) ----
class _Val:
    def __init__(self, value):
        self.value = value


class AudioFormats:
    WAV = _Val("wav")


class AudioCodecs:
    PCM = _Val("pcm")


class AudioBitRates:
    BITRATE_16 = _Val("16")


class AudioSampleRates:
    SAMPLE_RATE_16000 = _Val("16000")


class AudioChannels:
    CHANNEL_1 = _Val("1")


class SpeechResultState:
    SUCCESS = "success"
    ERROR = "error"


class SpeechResult:
    """Mirrors homeassistant.components.stt.SpeechResult (2026.10.4)."""

    def __init__(self, text=None, result=None):
        self.text = text
        self.result = result


class SpeechMetadata:
    def __init__(self, language, sample_rate, bit_rate, channel):
        self.language = language
        self.sample_rate = sample_rate
        self.bit_rate = bit_rate
        self.channel = channel


ha_components_stt = types.ModuleType("homeassistant.components.stt")
ha_components_stt.SpeechToTextEntity = object
ha_components_stt.SpeechMetadata = SpeechMetadata
ha_components_stt.SpeechResult = SpeechResult
ha_components_stt.SpeechResultState = SpeechResultState
ha_components_stt.AudioFormats = AudioFormats
ha_components_stt.AudioCodecs = AudioCodecs
ha_components_stt.AudioBitRates = AudioBitRates
ha_components_stt.AudioSampleRates = AudioSampleRates
ha_components_stt.AudioChannels = AudioChannels

ha_components_tts = types.ModuleType("homeassistant.components.tts")
ha_components_tts.TextToSpeechEntity = object
ha_components_tts.TtsAudioType = tuple

ha_core.callback = lambda f: f
ha_entity_platform = types.ModuleType("homeassistant.helpers.entity_platform")
ha_entity_platform.AddConfigEntryEntitiesCallback = object

sys.modules["homeassistant.components.stt"] = ha_components_stt
sys.modules["homeassistant.components.tts"] = ha_components_tts
sys.modules["homeassistant.helpers.entity_platform"] = ha_entity_platform

sys.modules["homeassistant"] = ha
sys.modules["homeassistant.components"] = ha_components
sys.modules["homeassistant.components.conversation"] = ha_components_conversation
sys.modules["homeassistant.core"] = ha_core
sys.modules["homeassistant.exceptions"] = ha_exceptions
sys.modules["homeassistant.helpers"] = ha_helpers
sys.modules["homeassistant.helpers.device_registry"] = ha_dr
sys.modules["homeassistant.helpers.llm"] = ha_llm
sys.modules["homeassistant.helpers.entity"] = ha_entity
sys.modules["homeassistant.helpers.json"] = ha_json

probatio_stub = types.ModuleType("probatio")
probatio_stub.to_openapi = lambda schema, custom_serializer=None, openapi_version=None: schema
sys.modules["probatio"] = probatio_stub

# Python 3.12 in this sandbox lacks guess_file_type (added in 3.13, which HA
# 2026.10 requires). Stub it for the test only.
import mimetypes

if not hasattr(mimetypes, "guess_file_type"):
    mimetypes.guess_file_type = lambda path: (mimetypes.guess_type(path)[0], None)

aiohttp_stub = types.ModuleType("aiohttp")


class ClientSession:
    pass


def ClientTimeout(total=None):
    return ("timeout", total)


class ClientError(Exception):
    pass


class ClientResponse:
    pass


aiohttp_stub.ClientSession = ClientSession
aiohttp_stub.ClientTimeout = ClientTimeout
aiohttp_stub.ClientError = ClientError
aiohttp_stub.ClientResponse = ClientResponse


class FormData:
    """Minimal multipart stub: records fields for assertions."""

    def __init__(self):
        self.fields = {}

    def add_field(self, name, value, **kwargs):
        self.fields[name] = value


aiohttp_stub.FormData = FormData
sys.modules["aiohttp"] = aiohttp_stub

# ---- Load the component without executing its HA-dependent __init__.py ----
pkg = types.ModuleType("llm_conversation")
from pathlib import Path

pkg.__path__ = [
    str(Path(__file__).resolve().parent / "custom_components" / "llm_conversation")
]
sys.modules["llm_conversation"] = pkg

const = importlib.import_module("llm_conversation.const")
entity_mod = importlib.import_module("llm_conversation.entity")
anthropic_mod = importlib.import_module("llm_conversation.providers.anthropic")
google_mod = importlib.import_module("llm_conversation.providers.google")
openai_mod = importlib.import_module("llm_conversation.providers.openai_compat")
pkg.LLMConversationConfigEntry = object
mistral_mod = importlib.import_module("llm_conversation.providers.mistral")
stt_mod = importlib.import_module("llm_conversation.stt")
tts_mod = importlib.import_module("llm_conversation.tts")


# ---- Fake SSE infrastructure ----
class LineIter:
    def __init__(self, lines):
        self._lines = lines
        self._i = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i >= len(self._lines):
            raise StopAsyncIteration
        line = self._lines[self._i]
        self._i += 1
        return line.encode() if isinstance(line, str) else line


class FakeResp:
    def __init__(self, lines=None, status=200):
        self.status = status
        self.content = LineIter(lines or [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self):
        return ""

    async def json(self):
        return {}


class FakeSession:
    def __init__(self, lines=None, status=200, json_data=None):
        self._lines = lines or []
        self._status = status
        self._json_data = json_data

    def get(self, url, **kwargs):
        resp = FakeResp(status=self._status)
        resp.json = lambda: _async_return(self._json_data)
        return resp

    def post(self, url, **kwargs):
        return FakeResp(self._lines, self._status)


async def _async_return(data):
    return data


async def collect(agen):
    return [item async for item in agen]


# ---- Test 1: _convert_content_to_param ----
result_content = type("R", (), {"data": {"ok": True}, "error": None})()
contents = [
    Content("system", "You are Home Assistant."),
    Content("user", "Zet het licht aan"),
    AssistantContent(
        "assistant", "", [ToolInput("call_0", "HassTurnOn", {"name": "light.woonkamer"})]
    ),
    ToolResultContent("call_0", "HassTurnOn", result_content),
]
messages = entity_mod._convert_content_to_param(contents)
assert messages[0] == {"role": "system", "content": "You are Home Assistant."}, messages[0]
assert messages[1] == {"role": "user", "content": "Zet het licht aan"}
assert messages[2]["tool_calls"][0]["function"]["name"] == "HassTurnOn"
assert messages[2]["role"] == "assistant"
assert messages[3]["role"] == "tool" and messages[3]["tool_call_id"] == "call_0"
print("TEST 1 OK: content -> canonical messages")

# ---- Test 2: Anthropic message conversion ----
client = anthropic_mod.AnthropicClient(None, "key")
system, converted = client._convert_messages(messages)
assert system == ["You are Home Assistant."]
assert converted[0] == {"role": "user", "content": [{"type": "text", "text": "Zet het licht aan"}]}
# assistant + tool result should be merged into ONE user turn after assistant
assert converted[1]["role"] == "assistant"
assert converted[1]["content"][0] == {
    "type": "tool_use", "id": "call_0", "name": "HassTurnOn",
    "input": {"name": "light.woonkamer"},
}
assert converted[2]["role"] == "user" and converted[2]["content"][0]["type"] == "tool_result"
print("TEST 2 OK: Anthropic message conversion")

# ---- Test 3: Anthropic SSE stream through entity transform ----
sse = [
    'event: message_start',
    'data: {"type":"message_start","message":{"role":"assistant"}}',
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_1","name":"HassTurnOn"}}',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"name\\":\\"light"}}',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":".woonkamer\\"}"}}',
    'data: {"type":"content_block_start","index":1,"content_block":{"type":"text"}}',
    'data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"Doe ik!"}}',
    'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}',
]
anthropic_client = anthropic_mod.AnthropicClient(FakeSession(sse), "key")
payload = anthropic_client.build_payload(
    model="claude-sonnet-5-5", messages=messages, tools=[],
    temperature=0.2, max_tokens=1024,
)
assert "temperature" not in payload, "temperature must not be sent to Anthropic"
assert payload["max_tokens"] == 1024


class FakeEntity:
    _transform_stream = entity_mod.LLMConversationBaseEntity._transform_stream
    _flush_tool_calls = staticmethod(entity_mod.LLMConversationBaseEntity._flush_tool_calls)


deltas = asyncio.run(collect(FakeEntity._transform_stream(
    FakeEntity(), anthropic_client.async_stream_chat(payload)
)))
roles = [d for d in deltas if d.get("role") == "assistant"]
contents_out = [d for d in deltas if "content" in d]
tools_out = [d for d in deltas if "tool_calls" in d]
assert len(roles) == 1, roles
assert contents_out == [{"content": "Doe ik!"}], contents_out
assert len(tools_out) == 1
ti = tools_out[0]["tool_calls"][0]
assert isinstance(ti, ToolInput) and ti.id == "toolu_1" and ti.tool_name == "HassTurnOn"
assert ti.tool_args == {"name": "light.woonkamer"}, ti.tool_args
print("TEST 3 OK: Anthropic SSE -> ToolInput (args:", ti.tool_args, ")")

# ---- Test 4: OpenAI-compatible stream (Mistral style) ----
sse2 = [
    'data: {"choices":[{"delta":{"role":"assistant"}}]}',
    'data: {"choices":[{"delta":{"content":"Prima"}}]}',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_9","type":"function","function":{"name":"HassSetState","arguments":"{\\"state\\":\\""}}]}}]}',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"on\\"}"}}]}}]}',
    'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
    'data: [DONE]',
]
mistral_client = openai_mod.OpenAICompatibleClient(FakeSession(sse2), "key", "https://api.mistral.ai/v1")
payload2 = mistral_client.build_payload(
    model="mistral-small-2603", messages=messages, tools=[], temperature=0.2, max_tokens=1024,
)
deltas2 = asyncio.run(collect(FakeEntity._transform_stream(
    FakeEntity(), mistral_client.async_stream_chat(payload2)
)))
tools2 = [d for d in deltas2 if "tool_calls" in d]
assert len(tools2) == 1
ti2 = tools2[0]["tool_calls"][0]
assert ti2.id == "call_9" and ti2.tool_name == "HassSetState" and ti2.tool_args == {"state": "on"}
assert {"content": "Prima"} in deltas2
print("TEST 4 OK: Mistral SSE -> ToolInput (args:", ti2.tool_args, ")")

# ---- Test 5: Gemini payload conversion ----
gemini_client = google_mod.GeminiClient(None, "key")
payload3 = gemini_client.build_payload(
    model="gemini-3.8-flash", messages=messages, tools=[{
        "type": "function", "function": {"name": "HassTurnOn", "description": "d", "parameters": {"type": "object"}}
    }], temperature=0.2, max_tokens=1024,
)
assert payload3["systemInstruction"]["parts"][0]["text"] == "You are Home Assistant."
assert payload3["contents"][0] == {"role": "user", "parts": [{"text": "Zet het licht aan"}]}
assert payload3["contents"][1]["parts"][0]["functionCall"] == {"name": "HassTurnOn", "args": {"name": "light.woonkamer"}}
assert payload3["contents"][2]["parts"][0]["functionResponse"]["name"] == "HassTurnOn"
assert payload3["contents"][2]["parts"][0]["functionResponse"]["response"]["result"]["data"] == {"ok": True}
assert payload3["tools"][0]["functionDeclarations"][0]["name"] == "HassTurnOn"
print("TEST 5 OK: Gemini payload conversion")

# ---- Test 6: Gemini SSE stream ----
sse3 = [
    'data: {"candidates":[{"content":{"role":"model","parts":[{"functionCall":{"name":"HassTurnOn","args":{"name":"light.keuken"}}}]}}]}',
    'data: {"candidates":[{"content":{"role":"model","parts":[{"text":"Aan!"}]},"finishReason":"STOP"}]}',
]
gemini_client2 = google_mod.GeminiClient(FakeSession(sse3), "key")
payload4 = dict(payload3)
deltas3 = asyncio.run(collect(FakeEntity._transform_stream(
    FakeEntity(), gemini_client2.async_stream_chat(payload4)
)))
tools3 = [d for d in deltas3 if "tool_calls" in d]
assert len(tools3) == 1
ti3 = tools3[0]["tool_calls"][0]
assert ti3.tool_name == "HassTurnOn" and ti3.tool_args == {"name": "light.keuken"}
assert {"content": "Aan!"} in deltas3
print("TEST 6 OK: Gemini SSE -> ToolInput")

# ---- Test 7: Voxtral WAV header builder ----
metadata = SpeechMetadata(
    language="nl-NL",
    sample_rate=_Val("16000"),
    bit_rate=_Val("16"),
    channel=_Val("1"),
)
pcm = b"\x01\x00" * 8  # 16 bytes of PCM
wav = stt_mod._build_wav(pcm, metadata)
assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
fields = struct.unpack("<4sI4s4sIHHIIHH4sI", wav[:44])
assert fields[0] == b"RIFF" and fields[1] == 36 + len(pcm)
assert fields[2] == b"WAVE" and fields[3] == b"fmt "
assert fields[5] == 1 and fields[6] == 1  # PCM, mono
assert fields[7] == 16000 and fields[8] == 32000 and fields[9] == 2
assert fields[10] == 16 and fields[11] == b"data" and fields[12] == len(pcm)
assert wav[44:] == pcm
print("TEST 7 OK: WAV header (16 kHz, 16-bit, mono)")

# ---- Test 8: MistralClient.async_transcribe (multipart) ----
class CaptureSession:
    def __init__(self, json_data):
        self._json_data = json_data
        self.captured = []

    def post(self, url, **kwargs):
        self.captured.append((url, kwargs))
        resp = FakeResp(status=200)
        resp.json = lambda: _async_return(self._json_data)
        return resp


capture = CaptureSession({"text": "zet het licht aan"})
mistral_speech_client = mistral_mod.MistralClient(capture, "test-key")
transcript = asyncio.run(
    mistral_speech_client.async_transcribe(wav, "voxtral-mini-latest")
)
url, kwargs = capture.captured[0]
assert url == "https://api.mistral.ai/v1/audio/transcriptions"
form = kwargs["data"]
assert isinstance(form, FormData)
assert form.fields["model"] == "voxtral-mini-latest"
assert form.fields["file"] == wav
assert kwargs["headers"] == {"Authorization": "Bearer test-key"}
assert transcript == "zet het licht aan"
print("TEST 8 OK: Voxtral transcription payload + response")

# ---- Test 9: MistralClient.async_speech (JSON + base64 audio) ----
import base64 as _base64

audio_bytes = b"ID3 fake mp3 bytes"
capture2 = CaptureSession(
    {"audio_data": _base64.b64encode(audio_bytes).decode()}
)
speech_client = mistral_mod.MistralClient(capture2, "test-key")
out = asyncio.run(
    speech_client.async_speech("Doe ik!", "voxtral-mini-tts-3-0", "voice-123")
)
url2, kwargs2 = capture2.captured[0]
assert url2 == "https://api.mistral.ai/v1/audio/speech"
payload5 = kwargs2["json"]
assert payload5["model"] == "voxtral-mini-tts-3-0"
assert payload5["input"] == "Doe ik!"
assert payload5["voice_id"] == "voice-123"
assert payload5["response_format"] == "mp3"
assert out == audio_bytes

# Without a voice ID the parameter must be omitted entirely.
capture3 = CaptureSession({"audio_data": _base64.b64encode(b"x").decode()})
speech_client2 = mistral_mod.MistralClient(capture3, "test-key")
asyncio.run(speech_client2.async_speech("Hallo", "voxtral-mini-tts-2603"))
assert "voice_id" not in capture3.captured[0][1]["json"]
print("TEST 9 OK: Voxtral TTS payload + base64 decode")

# ---- Test 10: STT entity end-to-end (chunks -> WAV -> result) ----
class FakeEntry:
    entry_id = "abc123"
    title = "Mistral"
    data = {"provider": "mistral"}
    options = {}
    runtime_data = mistral_speech_client


class FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


capture_stt = CaptureSession({"text": "goedemorgen"})
stt_client = mistral_mod.MistralClient(capture_stt, "test-key")
stt_entry = FakeEntry()
stt_entry.options = {"stt_model": "voxtral-small-transcribe-3-0"}
stt_entry.runtime_data = stt_client
stt_entity = stt_mod.MistralVoxtralSTTEntity(stt_entry)
assert stt_entity.check_metadata(metadata) is True
result = asyncio.run(
    stt_entity.async_process_audio_stream(
        metadata, FakeStream([b"\x01\x00\x02\x00", b"\x03\x00\x04\x00"])
    )
)
assert result.result == SpeechResultState.SUCCESS
assert result.text == "goedemorgen"
sent_form = capture_stt.captured[0][1]["data"]
assert sent_form.fields["model"] == "voxtral-small-transcribe-3-0"
assert sent_form.fields["file"].startswith(b"RIFF")
print("TEST 10 OK: STT entity pipeline (chunks -> WAV -> Voxtral -> text)")

# ---- Test 11: TTS entity end-to-end ----
capture_tts = CaptureSession({"audio_data": _base64.b64encode(b"audio").decode()})
tts_client = mistral_mod.MistralClient(capture_tts, "test-key")
tts_entry = FakeEntry()
tts_entry.options = {
    "tts_model": "voxtral-mini-tts-2603",
    "tts_voice_id": "my-voice",
}
tts_entry.runtime_data = tts_client
tts_entity = tts_mod.MistralVoxtralTTSEntity(tts_entry)
fmt, audio_out = asyncio.run(
    tts_entity.async_get_tts_audio("Test", "nl", {})
)
assert fmt == "mp3" and audio_out == b"audio"
sent_payload = capture_tts.captured[0][1]["json"]
assert sent_payload["voice_id"] == "my-voice"
assert sent_payload["model"] == "voxtral-mini-tts-2603"
print("TEST 11 OK: TTS entity pipeline (text -> Voxtral -> mp3 bytes)")

# ---- Test 12: token usage reaches the conversation trace ----
class FakeTraceLog:
    def __init__(self):
        self.traces = []

    def async_trace(self, data):
        self.traces.append(data)


sse_usage = [
    'data: {"choices":[{"delta":{"content":"Hoi"}}]}',
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
    'data: {"choices":[],"usage":{"prompt_tokens":812,"completion_tokens":5}}',
    'data: [DONE]',
]
usage_client = openai_mod.OpenAICompatibleClient(FakeSession(sse_usage), "key", "https://x/v1")
trace_log = FakeTraceLog()
passed = asyncio.run(collect(entity_mod.LLMConversationBaseEntity._trace_usage(
    trace_log, usage_client.async_stream_chat(payload2)
)))
assert trace_log.traces == [{"stats": {"input_tokens": 812, "output_tokens": 5}}], trace_log.traces
assert not any("usage" in chunk for chunk in passed), "usage chunks must not reach the chat log"
print("TEST 12 OK: token usage -> trace stats")

# ---- Test 13: truncated answer (max tokens) raises instead of silently cutting off ----
sse_len = [
    'data: {"choices":[{"delta":{"content":"Half antw"}}]}',
    'data: {"choices":[{"delta":{},"finish_reason":"length"}]}',
]
len_client = openai_mod.OpenAICompatibleClient(FakeSession(sse_len), "key", "https://x/v1")
try:
    asyncio.run(collect(FakeEntity._transform_stream(
        FakeEntity(), len_client.async_stream_chat(payload2)
    )))
except HomeAssistantError as err:
    assert "maximum number of tokens" in str(err)
else:
    raise AssertionError("finish_reason=length must raise")
print("TEST 13 OK: max-tokens truncation is reported")

# ---- Test 14: Anthropic prompt caching + usage ----
cache_payload = anthropic_client.build_payload(
    model="claude-sonnet-5-5", messages=messages,
    tools=[
        {"type": "function", "function": {"name": "A", "description": "a", "parameters": {"type": "object"}}},
        {"type": "function", "function": {"name": "B", "description": "b", "parameters": {"type": "object"}}},
    ],
    temperature=0.2, max_tokens=1024,
)
assert "cache_control" not in cache_payload["tools"][0]
assert cache_payload["tools"][-1]["cache_control"] == {"type": "ephemeral"}
assert cache_payload["system"][0]["cache_control"] == {"type": "ephemeral"}
assert cache_payload["system"][0]["text"] == "You are Home Assistant."
sse_au = [
    'data: {"type":"message_start","message":{"usage":{"input_tokens":20,"cache_read_input_tokens":900}}}',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Ok"}}',
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":3}}',
]
au_log = FakeTraceLog()
asyncio.run(collect(entity_mod.LLMConversationBaseEntity._trace_usage(
    au_log, anthropic_mod.AnthropicClient(FakeSession(sse_au), "key").async_stream_chat(cache_payload)
)))
assert au_log.traces == [{"stats": {"input_tokens": 920, "output_tokens": 3}}], au_log.traces
print("TEST 14 OK: Anthropic prompt caching markers + usage")

# ---- Test 15: Gemini tool-call ids are unique across tool-call rounds ----
ids = []
for _ in range(2):
    g = google_mod.GeminiClient(FakeSession(list(sse3)), "key")
    ds = asyncio.run(collect(FakeEntity._transform_stream(FakeEntity(), g.async_stream_chat(payload4))))
    ids += [d["tool_calls"][0].id for d in ds if "tool_calls" in d]
assert len(ids) == 2 and ids[0] != ids[1], ids
print("TEST 15 OK: Gemini tool-call ids unique per round")

# ---- Test 16: empty transcription is an STT error, not an empty command ----
empty_entry = FakeEntry()
empty_entry.options = {}
empty_entry.runtime_data = mistral_mod.MistralClient(CaptureSession({"text": "  "}), "k")
empty_result = asyncio.run(
    stt_mod.MistralVoxtralSTTEntity(empty_entry).async_process_audio_stream(
        metadata, FakeStream([b"\x00\x00"])
    )
)
assert empty_result.result == SpeechResultState.ERROR and empty_result.text is None
print("TEST 16 OK: empty transcription -> STT error")

print("ALL SMOKE TESTS PASSED")
