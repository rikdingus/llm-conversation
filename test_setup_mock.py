"""Offline test for scripts/setup_assist.py against a mock Home Assistant.

The mock mirrors the HA 2026.10.4 API shapes the script depends on
(verified against the core source, see AGENTS.md):

- GET /api/ answers {"message": "API running."}
- the config-entry list never contains entry "data"
- a failed flow step re-sends the form with the submitted values as
  suggested values (so a careless error message would leak the API key)
- REST and websocket share one port; the websocket handshake response
  and "auth_required" arrive in a single packet
- assist_pipeline/pipeline/{create,update} require every pipeline field
  (incl. "name"), update and set_preferred take "pipeline_id", and the
  list answers with "preferred_pipeline"

Run from the repository root: python3 test_setup_mock.py
"""
from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
import os
import struct
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_here = Path(__file__).resolve().parent
SCRIPT = _here / "scripts" / "setup_assist.py"
spec = importlib.util.spec_from_file_location("setup_assist", SCRIPT)
sa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sa)

TOKEN = "test-ha-token-0123456789"
GOOD_KEY = "test-mistral-key-abcdef"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
DEFAULT_PROMPT = "You are a voice assistant for Home Assistant."

PIPELINE_REQUIRED = {
    "conversation_engine", "conversation_language", "language", "name",
    "stt_engine", "stt_language", "tts_engine", "tts_language", "tts_voice",
    "wake_word_entity", "wake_word_id",
}
PIPELINE_OPTIONAL = {"prefer_local_intents", "acknowledge_media_id"}
CLAUDE_PIPELINE = {
    "id": "p-claude", "name": "Claude",
    "conversation_engine": "conversation.claude_conversation",
    "conversation_language": "*", "language": "nl",
    "stt_engine": "stt.google_ai_stt", "stt_language": "nl-NL",
    "tts_engine": "tts.google_ai_tts", "tts_language": "nl-NL",
    "tts_voice": "iapetus", "wake_word_entity": None, "wake_word_id": None,
    "prefer_local_intents": True,
}


# ------------------------------------------------------------- mock state --


class State:
    def __init__(self) -> None:
        self.entries: list[dict] = []        # what HA stores (incl. data)
        self.options: dict[str, dict] = {}   # entry_id -> options
        self.registry: list[dict] = [
            # decoy from another integration with a name the old,
            # suffix-based lookup would have picked up
            {"entity_id": "conversation.old_mistral_ai", "platform": "other",
             "config_entry_id": "other-entry", "disabled_by": None},
        ]
        self.states: dict[str, str] = {"conversation.old_mistral_ai": "unknown"}
        self.flows: dict[str, dict] = {}
        self.aborted: list[str] = []
        self.pipelines: list[dict] = [copy.deepcopy(CLAUDE_PIPELINE)]
        self.preferred = "p-claude"
        self.counter = 0

    def next_id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{self.counter}"

    def create_entry(self, api_key: str) -> dict:
        entry_id = self.next_id("entry")
        entry = {"entry_id": entry_id, "domain": "llm_conversation",
                 "title": "Mistral AI", "state": "loaded",
                 "data": {"provider": "mistral", "api_key": api_key}}
        self.entries.append(entry)
        suffix = "" if len(self.entries) == 1 else f"_{len(self.entries)}"
        for eid in (f"conversation.mistral_ai{suffix}",
                    f"stt.mistral_ai_speech_to_text{suffix}",
                    f"tts.mistral_ai_text_to_speech{suffix}"):
            self.registry.append({"entity_id": eid, "platform": "llm_conversation",
                                  "config_entry_id": entry_id, "disabled_by": None})
            self.states[eid] = "unknown"
        return entry

    def reload(self, entry_id: str) -> None:
        """Options saved: entities are unavailable for a moment."""
        ids = [r["entity_id"] for r in self.registry if r["config_entry_id"] == entry_id]
        for eid in ids:
            self.states[eid] = "unavailable"

        def back() -> None:
            for eid in ids:
                self.states[eid] = "unknown"
        timer = threading.Timer(1.5, back)
        timer.daemon = True
        timer.start()


S = State()


def options_form(entry_id: str) -> list[dict]:
    o = S.options.get(entry_id, {})

    def field(name: str, suggested: object) -> dict:
        return {"name": name, "optional": True,
                "description": {"suggested_value": suggested}}

    return [
        field("prompt", o.get("prompt", DEFAULT_PROMPT)),
        field("llm_hass_api", o.get("llm_hass_api")),
        field("chat_model", o.get("chat_model", "mistral-small-latest")),
        field("max_tokens", o.get("max_tokens", 3000)),
        field("temperature", o.get("temperature", 0.3)),
        field("stt_model", o.get("stt_model", "voxtral-mini-latest")),
        field("tts_model", o.get("tts_model", "voxtral-mini-tts-3-0")),
        field("tts_voice_id", o.get("tts_voice_id", "")),
    ]


# ------------------------------------------------------- websocket server --


def ws_frame(obj: dict) -> bytes:
    payload = json.dumps(obj).encode()
    n = len(payload)
    if n < 126:
        header = bytes([0x81, n])
    elif n < 65536:
        header = bytes([0x81, 126]) + struct.pack(">H", n)
    else:
        header = bytes([0x81, 127]) + struct.pack(">Q", n)
    return header + payload


def ws_read(rfile) -> dict | None:
    """Next JSON message from the client; None when it closes."""
    while True:
        head = rfile.read(2)
        if len(head) < 2:
            return None
        opcode, n = head[0] & 0x0F, head[1] & 0x7F
        assert head[1] & 0x80, "client frames must be masked"
        if n == 126:
            n = struct.unpack(">H", rfile.read(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", rfile.read(8))[0]
        mask = rfile.read(4)
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(rfile.read(n)))
        if opcode == 8:
            return None
        if opcode == 10:  # pong for our ping
            PONGS.append(data)
            continue
        return json.loads(data.decode())


PONGS: list[bytes] = []


def ws_handle(msg: dict) -> tuple[bool, object]:
    kind = msg.get("type")
    if kind == "config/entity_registry/list":
        return True, S.registry
    if kind == "assist_pipeline/pipeline/list":
        return True, {"pipelines": S.pipelines, "preferred_pipeline": S.preferred}
    if kind in ("assist_pipeline/pipeline/create", "assist_pipeline/pipeline/update"):
        fields = {k: v for k, v in msg.items() if k not in ("id", "type", "pipeline_id")}
        missing = PIPELINE_REQUIRED - set(fields)
        unknown = set(fields) - PIPELINE_REQUIRED - PIPELINE_OPTIONAL
        if missing or unknown:
            return False, {"code": "invalid_format",
                           "message": f"missing {sorted(missing)}, unknown {sorted(unknown)}"}
        if kind.endswith("create"):
            item = {"id": S.next_id("p-"), **fields}
            S.pipelines.append(item)
            return True, item
        target = next((p for p in S.pipelines if p["id"] == msg.get("pipeline_id")), None)
        if target is None:
            return False, {"code": "not_found", "message": "Unable to find pipeline_id"}
        target.clear()
        target.update({"id": msg["pipeline_id"], **fields})
        return True, target
    if kind == "assist_pipeline/pipeline/set_preferred":
        if "pipeline_id" not in msg:
            return False, {"code": "invalid_format", "message": "required key pipeline_id"}
        S.preferred = msg["pipeline_id"]
        return True, None
    return False, {"code": "unknown_command", "message": kind}


# ------------------------------------------------------------ HTTP server --


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def _json(self, obj: object, status: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._json({"message": "Unauthorized"}, 401)
            return False
        return True

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:
        if self.path == "/api/websocket":
            return self._websocket()
        if not self._authorized():
            return
        if self.path == "/api/":
            self._json({"message": "API running."})
        elif self.path.startswith("/api/config/config_entries/entry"):
            # like entry.as_json_fragment: no "data"
            self._json([{k: v for k, v in e.items() if k != "data"} for e in S.entries])
        elif self.path.startswith("/api/states/"):
            eid = self.path.rsplit("/", 1)[1]
            if eid in S.states:
                self._json({"entity_id": eid, "state": S.states[eid]})
            else:
                self._json({"message": "Entity not found."}, 404)
        else:
            self._json({"message": "not found"}, 404)

    def do_DELETE(self) -> None:
        if not self._authorized():
            return
        flow_id = self.path.rsplit("/", 1)[1]
        S.flows.pop(flow_id, None)
        S.aborted.append(flow_id)
        self._json({"message": "Flow aborted"})

    def do_POST(self) -> None:  # noqa: C901
        if not self._authorized():
            return
        body = self._body()
        if self.path == "/api/config/config_entries/flow":
            assert body == {"handler": "llm_conversation"}, body
            fid = S.next_id("flow")
            S.flows[fid] = {"step": "user"}
            return self._json({"type": "form", "flow_id": fid, "handler": "llm_conversation",
                               "step_id": "user", "data_schema": [{"name": "provider"}],
                               "errors": None})
        if self.path.startswith("/api/config/config_entries/flow/"):
            fid = self.path.rsplit("/", 1)[1]
            flow = S.flows[fid]
            if flow["step"] == "user":
                assert body == {"provider": "mistral"}, body
                flow["step"] = "api_key"
                return self._json({"type": "form", "flow_id": fid, "step_id": "api_key",
                                   "data_schema": [{"name": "api_key"}], "errors": None})
            assert set(body) == {"api_key"}, body
            if body["api_key"] != GOOD_KEY:
                # like add_suggested_values_to_schema(): the key comes back
                return self._json({
                    "type": "form", "flow_id": fid, "handler": "llm_conversation",
                    "step_id": "api_key",
                    "data_schema": [{"name": "api_key", "required": True,
                                     "description": {"suggested_value": body["api_key"]}}],
                    "errors": {"base": "invalid_auth"},
                })
            entry = S.create_entry(body["api_key"])
            del S.flows[fid]
            return self._json({"type": "create_entry", "flow_id": fid, "title": entry["title"],
                               "result": {k: v for k, v in entry.items() if k != "data"}})
        if self.path == "/api/config/config_entries/options/flow":
            entry_id = body["handler"]
            fid = S.next_id("oflow")
            S.flows[fid] = {"entry_id": entry_id}
            return self._json({"type": "form", "flow_id": fid, "step_id": "init",
                               "data_schema": options_form(entry_id), "errors": None})
        if self.path.startswith("/api/config/config_entries/options/flow/"):
            fid = self.path.rsplit("/", 1)[1]
            entry_id = S.flows.pop(fid)["entry_id"]
            allowed = {f["name"] for f in options_form(entry_id)}
            if extra := set(body) - allowed:
                return self._json({"message": f"User input malformed: extra keys {extra}"}, 400)
            if body.get("llm_hass_api") is None:
                body.pop("llm_hass_api", None)
            S.options[entry_id] = body  # options are replaced, like HA
            S.reload(entry_id)
            return self._json({"type": "create_entry", "flow_id": fid, "title": ""})
        self._json({"message": "not found"}, 404)

    def _websocket(self) -> None:
        key = self.headers["Sec-WebSocket-Key"]
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        handshake = (
            "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode()
        # one write: handshake + first message in the same packet
        self.wfile.write(handshake + ws_frame({"type": "auth_required"}))
        auth = ws_read(self.rfile)
        if not auth or auth.get("access_token") != TOKEN:
            self.wfile.write(ws_frame({"type": "auth_invalid", "message": "Invalid access token"}))
            self.close_connection = True
            return
        # a ping before the first answer: the client must answer and go on
        self.wfile.write(ws_frame({"type": "auth_ok"}) + bytes([0x89, 0]))
        while (msg := ws_read(self.rfile)) is not None:
            ok, result = ws_handle(msg)
            reply = {"id": msg["id"], "type": "result", "success": ok}
            reply["result" if ok else "error"] = result
            self.wfile.write(ws_frame(reply))
        self.close_connection = True


server =ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{server.server_address[1]}"


def expect_error(fn, *args) -> str:
    try:
        fn(*args)
    except sa.SetupError as err:
        return str(err)
    raise AssertionError(f"{fn.__name__} should have failed")


def no_key() -> str:
    raise AssertionError("the Mistral key must not be asked for an existing entry")


# ------------------------------------------------------------------ tests --

# 1. API check: real response shape, and a wrong token
sa.step_api(BASE, TOKEN)
assert "401" in expect_error(sa.step_api, BASE, "wrong-token")
print("TEST 1 OK: API status + rejected token")

# 2. Full run through main(), as a user would: env vars, no terminal
env = {**os.environ, "HASS_URL": BASE + "/", "HASS_TOKEN": TOKEN,
       "MISTRAL_API_KEY": GOOD_KEY, "MISTRAL_VOICE_ID": "voice_3145"}
run = subprocess.run([sys.executable, "-I", str(SCRIPT)], env=env, stdin=subprocess.DEVNULL,
                     capture_output=True, text=True, timeout=60)
out = run.stdout + run.stderr
print("\n".join("    | " + line for line in out.splitlines()))
assert run.returncode == 0, out
assert TOKEN not in out and GOOD_KEY not in out, "secret printed"
entry_id = S.entries[0]["entry_id"]
opts = S.options[entry_id]
assert opts["llm_hass_api"] == ["assist"], opts
assert opts["tts_voice_id"] == "voice_3145", opts
assert opts["stt_model"] == "voxtral-mini-latest" and opts["tts_model"] == "voxtral-mini-tts-3-0"
assert opts["prompt"] == DEFAULT_PROMPT and opts["chat_model"] == "mistral-small-latest", opts
assert len(S.pipelines) == 2, S.pipelines
mistral = S.pipelines[1]
assert mistral["name"] == "Mistral" and S.preferred == mistral["id"]
assert mistral["conversation_engine"] == "conversation.mistral_ai"
assert mistral["stt_engine"] == "stt.mistral_ai_speech_to_text"
assert mistral["tts_engine"] == "tts.mistral_ai_text_to_speech"
assert (mistral["language"], mistral["conversation_language"], mistral["stt_language"],
        mistral["tts_language"]) == ("nl", "*", "nl-NL", "nl-NL"), mistral
assert S.pipelines[0] == CLAUDE_PIPELINE, "existing pipeline was modified"
print("TEST 2 OK: fresh install via main() (entry, options, entities, new preferred pipeline)")

# 3. Second run: no new entry, no key prompt, UI changes and voice kept
S.options[entry_id]["chat_model"] = "mistral-large-latest"  # changed in the UI
S.pipelines[1]["prefer_local_intents"] = False              # changed in the UI
ws = sa.HaWebsocket(BASE, TOKEN)
try:
    assert sa.step_entry(BASE, TOKEN, ws, no_key) == entry_id
    sa.step_options(BASE, TOKEN, entry_id, {"stt_model": None, "tts_model": None,
                                            "tts_voice_id": None}, "assist")
    opts = S.options[entry_id]
    assert opts["chat_model"] == "mistral-large-latest", opts
    assert opts["tts_voice_id"] == "voice_3145", opts
    assert opts["llm_hass_api"] == ["assist"], opts
    entities = sa.step_wait_entities(BASE, TOKEN, ws, entry_id, 15)
    sa.step_pipeline(ws, entities, "Mistral", "nl-NL")
finally:
    ws.close()
assert len(S.entries) == 1 and len(S.pipelines) == 2, (S.entries, S.pipelines)
assert S.pipelines[1]["prefer_local_intents"] is False, S.pipelines[1]
assert S.pipelines[0] == CLAUDE_PIPELINE
assert PONGS, "client did not answer the server ping"
print("TEST 3 OK: rerun reuses entry + pipeline, keeps UI settings, answers pings")

# 4. Renamed entry is still recognised (via its STT entity)
S.entries[0]["title"] = "Mijn Mistral"
ws = sa.HaWebsocket(BASE, TOKEN)
try:
    assert sa.step_entry(BASE, TOKEN, ws, no_key) == entry_id
finally:
    ws.close()
print("TEST 4 OK: renamed entry recognised")

# 5. Wrong API key: clear error, key never echoed, flow aborted
S = State()
ws = sa.HaWebsocket(BASE, TOKEN)
try:
    message = expect_error(sa.step_entry, BASE, TOKEN, ws, lambda: "bad-secret-key-999")
finally:
    ws.close()
assert "invalid_auth" in message and "bad-secret-key-999" not in message, message
assert S.aborted and not S.flows and not S.entries, (S.aborted, S.flows)
print("TEST 5 OK: invalid key -> error without the key, flow aborted")

# 6. Websocket login with a wrong token is a clean SetupError
try:
    sa.HaWebsocket(BASE, "wrong-token")
except sa.SetupError as err:
    assert "geweigerd" in str(err), err
else:
    raise AssertionError("wrong websocket token accepted")
print("TEST 6 OK: websocket auth failure reported")

server.shutdown()
print("ALL SETUP-SCRIPT TESTS PASSED")
