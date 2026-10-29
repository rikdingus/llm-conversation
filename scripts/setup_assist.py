#!/usr/bin/env python3
"""Set up the LLM Conversation integration in a live Home Assistant.

Does everything after installing via HACS, driven entirely by the HA API:

  1. Creates the config entry (provider: Mistral AI, API key)
     via POST /api/config/config_entries/flow — reuses an existing entry
     if there is one.
  2. Sets the options (Assist device control, Voxtral STT/TTS models,
     voice ID) via the options flow REST endpoint.
  3. Waits for the conversation / stt / tts entities to appear.
  4. Points the preferred Assist pipeline at the three entities via the
     websocket API (assist_pipeline/pipeline/update).

Pure standard library (including a minimal WebSocket client), so this
runs anywhere Python 3.11+ runs — including the HA SSH/terminal add-on.

Usage:
  export HASS_URL=http://homeassistant.local:8123
  export HASS_TOKEN=<long-lived token: Profile -> Security>
  export MISTRAL_API_KEY=<key from console.mistral.ai>
  export MISTRAL_VOICE_ID=<voice id from studio.mistral.ai>
  python3 scripts/setup_assist.py

Secrets via environment variables on purpose: command-line arguments end
up in shell history and the process list.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import ssl
import struct
import sys
import time
import urllib.error
import urllib.request

DEFAULT_STT_MODEL = "voxtral-mini-latest"
DEFAULT_TTS_MODEL = "voxtral-mini-tts-3-0"
DOMAIN = "llm_conversation"


# ------------------------------------------------------------- HTTP helper --


def http_json(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: dict | None = None,
    timeout: int = 60,
) -> tuple[int, dict | str]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as err:
        raw, status = err.read(), err.code
    text = raw.decode("utf-8", "replace")
    try:
        return status, json.loads(text)
    except json.JSONDecodeError:
        return status, text


# -------------------------------------------------- Minimal WebSocket client --


class WebSocket:
    """Minimal RFC 6455 client, enough for the Home Assistant API."""

    def __init__(self, url: str, timeout: int = 60) -> None:
        self._timeout = timeout
        scheme, rest = url.split("://", 1)
        host_part, _, path = rest.partition("/")
        if ":" in host_part:
            host, port = host_part.split(":", 1)
        else:
            host, port = host_part, "443" if scheme == "https" else "80"
        self._host, self._port = host, int(port)
        self._tls = scheme == "https"
        self._path = "/" + path

    def connect(self) -> None:
        raw = socket.create_connection((self._host, self._port), timeout=self._timeout)
        if self._tls:
            ctx = ssl.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=self._host)
        self._sock = raw
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {self._path}/api/websocket HTTP/1.1\r\n"
            f"Host: {self._host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(request.encode())
        response = b""
        while b"\r\n\r\n" not in response:
            response += self._sock.recv(4096)
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise ConnectionError(f"WebSocket handshake failed: {response[:200]!r}")

    def _send_frame(self, payload: bytes, opcode: int = 1) -> None:
        header = bytes([0x80 | opcode])
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(header + mask + masked)

    def _recv_exact(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = self._sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError("connection closed")
            data += chunk
        return data

    def recv_json(self) -> dict:
        while True:
            head = self._recv_exact(2)
            opcode, n = head[0] & 0x0F, head[1] & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recv_exact(8))[0]
            if opcode == 8:  # close
                raise ConnectionError("server closed the websocket")
            if opcode in (9, 10):  # ping/pong
                if n:
                    self._recv_exact(n)
                continue
            payload = self._recv_exact(n)
            if opcode == 1:
                return json.loads(payload.decode())

    def send_json(self, msg: dict) -> None:
        self._send_frame(json.dumps(msg).encode())

    def close(self) -> None:
        try:
            self._send_frame(b"", opcode=8)
            self._sock.close()
        except OSError:
            pass


class HaWebsocket:
    """Home Assistant websocket session with request/response by id."""

    def __init__(self, url: str, token: str) -> None:
        self._ws = WebSocket(url)
        self._ws.connect()
        hello = self._ws.recv_json()
        if hello.get("type") != "auth_required":
            raise ConnectionError(f"unexpected hello: {hello}")
        self._ws.send_json({"type": "auth", "access_token": token})
        result = self._ws.recv_json()
        if result.get("type") != "auth_ok":
            raise PermissionError(f"auth failed: {result.get('message', result)}")
        self._id = 0

    def request(self, msg: dict) -> dict:
        self._id += 1
        msg = {"id": self._id, **msg}
        self._ws.send_json(msg)
        while True:
            data = self._ws.recv_json()
            if data.get("id") == self._id and ("result" in data or "success" in data):
                if not data.get("success"):
                    raise RuntimeError(f"websocket call failed: {data.get('error')}")
                return data["result"]
            # events from other subscriptions are ignored

    def close(self) -> None:
        self._ws.close()


# ------------------------------------------------------------------- steps --


def step_api(ha_url: str, token: str) -> None:
    status, data = http_json("GET", f"{ha_url}/api/", token=token)
    if status != 200 or not str(data).startswith("API running"):
        raise SystemExit(f"Kan Home Assistant niet bereiken via {ha_url} (HTTP {status}: {str(data)[:100]})")
    print(f"  [OK] Home Assistant bereikbaar: {str(data).strip()}")


def step_find_or_create_entry(ha_url: str, token: str, api_key: str) -> str:
    status, entries = http_json("GET", f"{ha_url}/api/config/config_entries/entry", token=token)
    if status == 200:
        existing = [
            e for e in (entries if isinstance(entries, list) else [])
            if e.get("domain") == DOMAIN
        ]
        mistral = [e for e in existing if e.get("data", {}).get("provider") == "mistral"]
        if mistral:
            entry = mistral[0]
            print(f"  [OK] Bestaande Mistral-entry hergebruikt: {entry['title']} ({entry['entry_id'][:8]}…)")
            return entry["entry_id"]
        if existing:
            print(f"  [INFO] Er is al een niet-Mistral entry ({existing[0]['title']}); ik maak er nog een aan voor Mistral.")

    # Start the config flow
    status, flow = http_json(
        "POST", f"{ha_url}/api/config/config_entries/flow",
        token=token, body={"handler": DOMAIN},
    )
    if status != 200 or flow.get("type") != "form":
        raise SystemExit(f"Config flow kon niet starten (HTTP {status}): {str(flow)[:200]}")

    # Step 1: provider
    status, flow = http_json(
        "POST", f"{ha_url}/api/config/config_entries/flow/{flow['flow_id']}",
        token=token, body={"provider": "mistral"},
    )
    if status != 200 or flow.get("type") != "form" or flow.get("step_id") != "api_key":
        raise SystemExit(f"Provider-stap mislukt (HTTP {status}): {str(flow)[:200]}")

    # Step 2: API key
    status, flow = http_json(
        "POST", f"{ha_url}/api/config/config_entries/flow/{flow['flow_id']}",
        token=token, body={"api_key": api_key},
    )
    if status != 200 or flow.get("type") != "create_entry":
        raise SystemExit(f"API-key-stap mislukt (HTTP {status}): {str(flow)[:200]}")
    print(f"  [OK] Config entry aangemaakt: {flow['title']}")
    return flow["result"]["entry_id"]


def step_options(ha_url: str, token: str, entry_id: str, opts: dict) -> None:
    status, flow = http_json(
        "POST", f"{ha_url}/api/config/config_entries/options/flow",
        token=token, body={"handler": entry_id},
    )
    if status != 200 or flow.get("type") != "form":
        raise SystemExit(f"Options flow kon niet starten (HTTP {status}): {str(flow)[:200]}")
    status, flow = http_json(
        "POST", f"{ha_url}/api/config/config_entries/options/flow/{flow['flow_id']}",
        token=token, body=opts,
    )
    if status != 200 or flow.get("type") != "create_entry":
        raise SystemExit(f"Options opslaan mislukt (HTTP {status}): {str(flow)[:200]}")
    print("  [OK] Opties opgeslagen (entry wordt herladen)")


def step_wait_entities(ha_url: str, token: str) -> dict[str, str]:
    """Poll until the three Mistral entities are available."""
    deadline = time.time() + 120
    wanted = {"conversation": None, "stt": None, "tts": None}
    while time.time() < deadline:
        status, states = http_json("GET", f"{ha_url}/api/states", token=token)
        if status == 200 and isinstance(states, list):
            for st in states:
                eid = st["entity_id"]
                domain, _, slug = eid.partition(".")
                if domain in wanted and slug.endswith(
                    ("mistral_ai", "mistral_ai_speech_to_text", "mistral_ai_text_to_speech")
                ):
                    wanted[domain] = eid
        if all(wanted.values()):
            for k, v in wanted.items():
                print(f"  [OK] {k}-entiteit: {v}")
            return wanted
        time.sleep(2)
    missing = [k for k, v in wanted.items() if not v]
    raise SystemExit(f"Entiteiten niet gevonden binnen 120 s (ontbrekend: {', '.join(missing)}). Herstart HA en draai dit script opnieuw.")


def step_pipeline(ha_url: str, token: str, entities: dict[str, str], pipeline_name: str | None, language: str) -> None:
    ws = HaWebsocket(ha_url, token)
    try:
        pipelines = ws.request({"type": "assist_pipeline/pipeline/list"})
        items = pipelines.get("pipelines", [])
        preferred = pipelines.get("preferred")
        target = None
        if pipeline_name:
            target = next((p for p in items if p["name"].lower() == pipeline_name.lower()), None)
        if target is None:
            target = next((p for p in items if p["id"] == preferred), None)
        if target is None:
            target = items[0]
        print(f"  [INFO] Pipeline: {target['name']}")
        updates = {
            "pipeline": target["id"],
            "conversation_engine": entities["conversation"],
            "conversation_language": language,
            "language": language,
            "stt_engine": entities["stt"],
            "stt_language": language,
            "tts_engine": entities["tts"],
            "tts_language": language,
            "tts_voice": None,
            "wake_word_entity": target.get("wake_word_entity"),
            "wake_word_id": target.get("wake_word_id"),
        }
        updated = ws.request({"type": "assist_pipeline/pipeline/update", **updates})
        print(f"  [OK] Pipeline '{updated['name']}' staat nu volledig op Mistral (STT → agent → TTS)")
        if preferred and target["id"] != preferred:
            ws.request({"type": "assist_pipeline/pipeline/set_preferred", "pipeline": target["id"]})
            print("  [OK] Pipeline ingesteld als voorkeurspipeline")
    finally:
        ws.close()


# -------------------------------------------------------------------- main --


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ha-url", default=os.environ.get("HASS_URL"), help="bijv. http://homeassistant.local:8123 (of env HASS_URL)")
    parser.add_argument("--ha-token", default=os.environ.get("HASS_TOKEN"), help="HA long-lived token. Liever via env HASS_TOKEN")
    parser.add_argument("--api-key", default=os.environ.get("MISTRAL_API_KEY"), help="Mistral API key. Liever via env MISTRAL_API_KEY")
    parser.add_argument("--voice-id", default=os.environ.get("MISTRAL_VOICE_ID"), help="Voice ID uit Mistral Studio. Liever via env MISTRAL_VOICE_ID")
    parser.add_argument("--stt-model", default=DEFAULT_STT_MODEL)
    parser.add_argument("--tts-model", default=DEFAULT_TTS_MODEL)
    parser.add_argument("--hass-api", default="assist", help="Home Assistant API voor apparaatbesturing (standaard 'assist')")
    parser.add_argument("--pipeline-name", default=None, help="een specifieke pipeline bij naam (standaard: de voorkeurspipeline)")
    parser.add_argument("--language", default="nl-NL", help="pijplijn-taal (standaard nl-NL)")
    args = parser.parse_args()

    ha_url = (args.ha_url or "").rstrip("/")
    if not ha_url or not args.ha_token or not args.api_key:
        print("Ontbrekende waarden: geef HASS_URL, HASS_TOKEN en MISTRAL_API_KEY op (env of argumenten).")
        return 2
    if not args.voice_id:
        print("Let op: geen voice-id opgegeven — TTS kan dan een 403/422 geven.\n")

    print("=== LLM Conversation: automatische installatie ===\n")

    print("[1/5] Home Assistant API")
    step_api(ha_url, args.ha_token)

    print("[2/5] Config entry (provider: Mistral AI)")
    entry_id = step_find_or_create_entry(ha_url, args.ha_token, args.api_key)

    print("[3/5] Opties (apparaatbesturing + Voxtral-modellen + stem)")
    step_options(
        ha_url, args.ha_token, entry_id,
        {
            "llm_hass_api": [args.hass_api],
            "stt_model": args.stt_model,
            "tts_model": args.tts_model,
            "tts_voice_id": args.voice_id or "",
        },
    )

    print("[4/5] Wachten op de entiteiten")
    entities = step_wait_entities(ha_url, args.ha_token)

    print("[5/5] Assist-pipeline koppelen")
    step_pipeline(ha_url, args.ha_token, entities, args.pipeline_name, args.language)

    print("\nKlaar! Vraag Assist iets: \"Wat is de temperatuur in de woonkamer?\"")
    print("Valideer eventueel met: python3 scripts/test_realtime.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
