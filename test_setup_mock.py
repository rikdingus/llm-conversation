"""Offline test for setup_assist.py against a mock Home Assistant server."""
import importlib.util
import json
import socket
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_here = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "sa", _here / "scripts" / "setup_assist.py"
)
sa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sa)

# ---------- mock state ----------
created_entries = []
flows = {}
flow_counter = [0]
options_saved = {}
states = []

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/":
            self._json("API running.")
        elif self.path == "/api/states":
            self._json(states)
        elif self.path == "/api/config/config_entries/entry":
            self._json(created_entries)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/config/config_entries/flow" and body.get("handler") == "llm_conversation":
            flow_counter[0] += 1
            fid = f"flow{flow_counter[0]}"
            flows[fid] = {"step": "user"}
            self._json({"type": "form", "flow_id": fid, "step_id": "user"})
        elif self.path.startswith("/api/config/config_entries/flow/flow"):
            fid = self.path.rsplit("/", 1)[1]
            flow = flows[fid]
            if flow["step"] == "user" and body == {"provider": "mistral"}:
                flow["step"] = "api_key"
                self._json({"type": "form", "flow_id": fid, "step_id": "api_key"})
            elif flow["step"] == "api_key" and body == {"api_key": "test-key"}:
                entry = {
                    "entry_id": "entry123", "domain": "llm_conversation",
                    "title": "Mistral AI",
                    "data": {"provider": "mistral", "api_key": "test-key"},
                }
                created_entries.append(entry)
                states.extend([
                    {"entity_id": "conversation.mistral_ai"},
                    {"entity_id": "stt.mistral_ai_speech_to_text"},
                    {"entity_id": "tts.mistral_ai_text_to_speech"},
                ])
                self._json({"type": "create_entry", "title": "Mistral AI", "result": entry})
            else:
                self._json({"errors": {"base": "bad input"}}, 400)
        elif self.path == "/api/config/config_entries/options/flow" and body.get("handler") == "entry123":
            flows["opts"] = True
            self._json({"type": "form", "flow_id": "opts", "step_id": "init"})
        elif self.path == "/api/config/config_entries/options/flow/opts":
            options_saved.update(body)
            self._json({"type": "create_entry", "data": body})
        else:
            self._json({"error": "not found"}, 404)

server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

# ---------- run the steps ----------
base = f"http://127.0.0.1:{port}"
sa.step_api(base, "tok")
entry_id = sa.step_find_or_create_entry(base, "tok", "test-key")
assert entry_id == "entry123", entry_id
sa.step_options(base, "tok", entry_id, {
    "llm_hass_api": ["assist"], "stt_model": "voxtral-mini-latest",
    "tts_model": "voxtral-mini-tts-3-0", "tts_voice_id": "JENNY",
})
assert options_saved["tts_voice_id"] == "JENNY", options_saved
entities = sa.step_wait_entities(base, "tok")
assert entities == {
    "conversation": "conversation.mistral_ai",
    "stt": "stt.mistral_ai_speech_to_text",
    "tts": "tts.mistral_ai_text_to_speech",
}, entities
print("HTTP steps OK:", entities)

# reuse path
entry_id2 = sa.step_find_or_create_entry(base, "tok", "test-key")
assert entry_id2 == "entry123" and len(created_entries) == 1
print("Entry reuse OK")

# ---------- websocket test against a minimal raw server ----------
def ws_server(sock_in):
    conn, _ = sock_in.accept()
    req = b""
    while b"\r\n\r\n" not in req:
        req += conn.recv(4096)
    conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: x\r\n\r\n")
    def send_json(obj):
        payload = json.dumps(obj).encode()
        header = bytes([0x81])
        n = len(payload)
        if n < 126:
            header += bytes([n])
        else:
            header += bytes([126]) + n.to_bytes(2, "big")
        conn.sendall(header + payload)
    def recv_json():
        head = conn.recv(2)
        n = head[1] & 0x7F
        if n == 126:
            n = int.from_bytes(conn.recv(2), "big")
        mask = conn.recv(4)
        data = bytearray(conn.recv(n))
        while len(data) < n:
            data += conn.recv(n - len(data))
        return json.loads(bytes(b ^ mask[i % 4] for i, b in enumerate(data)).decode())
    send_json({"type": "auth_required"})
    msg = recv_json()
    assert msg["type"] == "auth" and msg["access_token"] == "tok", msg
    send_json({"type": "auth_ok"})
    while True:
        try:
            msg = recv_json()
        except (ConnectionError, json.JSONDecodeError):
            break
        if msg.get("type") == "assist_pipeline/pipeline/list":
            send_json({"id": msg["id"], "type": "result", "success": True, "result": {
                "pipelines": [{"id": "p1", "name": "Home", "wake_word_entity": None, "wake_word_id": None}],
                "preferred": "p1",
            }})
        elif msg.get("type") == "assist_pipeline/pipeline/update":
            assert msg["conversation_engine"] == "conversation.mistral_ai"
            assert msg["stt_engine"] == "stt.mistral_ai_speech_to_text"
            assert msg["tts_engine"] == "tts.mistral_ai_text_to_speech"
            assert msg["language"] == "nl-NL"
            send_json({"id": msg["id"], "type": "result", "success": True, "result": {
                "id": "p1", "name": "Home", **{k: v for k, v in msg.items() if k not in ("id", "type")},
            }})
            break
    conn.close()

sock_in = socket.socket()
sock_in.bind(("127.0.0.1", 0))
sock_in.listen(1)
ws_port = sock_in.getsockname()[1]
threading.Thread(target=ws_server, args=(sock_in,), daemon=True).start()

sa.step_pipeline(f"http://127.0.0.1:{ws_port}", "tok", entities, None, "nl-NL")
print("WebSocket pipeline step OK")
print("ALL SETUP-SCRIPT TESTS PASSED")
