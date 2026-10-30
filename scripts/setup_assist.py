#!/usr/bin/env python3
"""Set up the LLM Conversation integration in a live Home Assistant.

Does everything after installing via HACS, driven entirely by the HA API:

  1. Checks that the HA API is reachable with the token.
  2. Reuses the existing Mistral config entry, or creates one via the
     config flow (POST /api/config/config_entries/flow).
  3. Saves the options (Assist device control, Voxtral STT/TTS models,
     voice ID) via the options flow. Values you did not pass on the
     command line (prompt, chat model, voice, ...) keep their current
     value, so running the script again never wipes your settings.
  4. Waits until the entry's conversation / stt / tts entities are
     available. They are looked up in the entity registry by config
     entry, so renamed entities and a second Mistral entry work too.
  5. Creates (or updates) a dedicated Assist pipeline, by default named
     "Mistral" (Voxtral STT -> Mistral agent -> Voxtral TTS), and makes
     it the preferred pipeline. Other pipelines are left untouched unless
     you name one with --pipeline-name.

Pure standard library (including a minimal WebSocket client), so this
runs anywhere Python 3.11+ runs, including the HA SSH/terminal add-on.

Usage:
  export HASS_URL=http://homeassistant.local:8123
  export MISTRAL_VOICE_ID=<voice id from Mistral Studio>
  python3 scripts/setup_assist.py

The HA token (HASS_TOKEN) and Mistral API key (MISTRAL_API_KEY) are read
from the environment or, when not set, asked for with hidden input.
They are deliberately not accepted as command-line arguments: those end
up in shell history and the process list. The Mistral key is only needed
when the config entry does not exist yet. The token must belong to an
administrator.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import socket
import ssl
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

DEFAULT_STT_MODEL = "voxtral-mini-latest"
DEFAULT_TTS_MODEL = "voxtral-mini-tts-3-0"
DEFAULT_PIPELINE_NAME = "Mistral"
DEFAULT_LANGUAGE = "nl-NL"
DOMAIN = "llm_conversation"
MISTRAL_TITLE = "Mistral AI"  # PROVIDER_LABELS["mistral"] in const.py
WANTED_DOMAINS = ("conversation", "stt", "tts")
ENTITY_TIMEOUT = 120
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC 6455

ERROR_HINTS = {
    "invalid_auth": "Mistral weigert de API-key.",
    "cannot_connect": "Home Assistant kan api.mistral.ai niet bereiken.",
    "unknown": "Onverwachte fout; kijk in het Home Assistant-log.",
}


class SetupError(Exception):
    """A step failed. The message never contains secrets."""


# ------------------------------------------------------------- HTTP helper --


def http_json(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: dict | None = None,
    timeout: int = 60,
) -> tuple[int, dict | list | str]:
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
    except (urllib.error.URLError, OSError) as err:
        reason = getattr(err, "reason", err)
        raise SetupError(f"Kan {url.split('?')[0]} niet bereiken: {reason}") from None
    text = raw.decode("utf-8", "replace")
    try:
        return status, json.loads(text)
    except json.JSONDecodeError:
        return status, text


def describe(status: int, flow: object) -> str:
    """Summarise a flow response WITHOUT its data_schema.

    On a failed step Home Assistant re-sends the form with the submitted
    values as suggested values, so the schema can contain the API key.
    """
    if isinstance(flow, dict):
        if flow.get("type") == "abort":
            return f"HTTP {status}, afgebroken: {flow.get('reason')}"
        if errors := flow.get("errors"):
            hints = [ERROR_HINTS.get(code, code) for code in errors.values()]
            return f"HTTP {status}, {errors} - {' '.join(hints)}"
        if "message" in flow:
            return f"HTTP {status}: {str(flow['message'])[:150]}"
        return f"HTTP {status}, type={flow.get('type')}, stap={flow.get('step_id')}"
    return f"HTTP {status}: {str(flow)[:150]}"


def is_flow(flow: object, flow_type: str, step_id: str | None = None) -> bool:
    return (
        isinstance(flow, dict)
        and flow.get("type") == flow_type
        and (step_id is None or flow.get("step_id") == step_id)
    )


def abort_flow(ha_url: str, token: str, path: str) -> None:
    """Best effort: abort a half-finished flow so it doesn't linger in HA."""
    try:
        http_json("DELETE", f"{ha_url}{path}", token=token, timeout=10)
    except SetupError:
        pass


# -------------------------------------------------- Minimal WebSocket client --


class WebSocket:
    """Minimal RFC 6455 client, enough for the Home Assistant API."""

    def __init__(self, ha_url: str, timeout: int = 60) -> None:
        parts = urllib.parse.urlsplit(ha_url)
        self._tls = parts.scheme == "https"
        self._host = parts.hostname or ""
        self._port = parts.port or (443 if self._tls else 80)
        self._netloc = parts.netloc
        # ha_url has no trailing slash; keep a reverse-proxy prefix if any
        self._path = parts.path.rstrip("/") + "/api/websocket"
        self._timeout = timeout
        self._buf = b""
        self._sock: socket.socket | None = None

    def connect(self) -> None:
        raw = socket.create_connection((self._host, self._port), timeout=self._timeout)
        if self._tls:
            ctx = ssl.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=self._host)
        self._sock = raw
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {self._path} HTTP/1.1\r\n"
            f"Host: {self._netloc}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(request.encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("verbinding gesloten tijdens de websocket-handshake")
            response += chunk
        # Home Assistant sends auth_required right after the 101: it can
        # arrive in the same packet, so keep whatever follows the headers.
        head, _, self._buf = response.partition(b"\r\n\r\n")
        status_line, *header_lines = head.decode("latin-1").split("\r\n")
        if " 101 " not in f"{status_line} ":
            raise ConnectionError(f"websocket-handshake mislukt: {status_line}")
        expected = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()
        ).decode()
        headers = {
            name.strip().lower(): value.strip()
            for name, _, value in (line.partition(":") for line in header_lines)
        }
        if headers.get("sec-websocket-accept") != expected:
            raise ConnectionError("websocket-handshake mislukt: ongeldige Sec-WebSocket-Accept")

    def _send_frame(self, payload: bytes, opcode: int = 1) -> None:
        assert self._sock is not None
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
        assert self._sock is not None
        while len(self._buf) < n:
            chunk = self._sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise ConnectionError("verbinding gesloten")
            self._buf += chunk
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def recv_json(self) -> dict:
        fragments: list[bytes] = []
        while True:
            b0, b1 = self._recv_exact(2)
            fin, opcode, n = b0 & 0x80, b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recv_exact(8))[0]
            payload = self._recv_exact(n)  # servers never mask frames
            if opcode == 8:  # close
                raise ConnectionError("Home Assistant heeft de websocket gesloten")
            if opcode == 9:  # ping: answer, or the server drops us
                self._send_frame(payload, opcode=10)
                continue
            if opcode == 10:  # pong
                continue
            fragments.append(payload)
            if fin:
                return json.loads(b"".join(fragments).decode())

    def send_json(self, msg: dict) -> None:
        self._send_frame(json.dumps(msg).encode())

    def close(self) -> None:
        try:
            self._send_frame(b"", opcode=8)
            if self._sock is not None:
                self._sock.close()
        except OSError:
            pass


class HaWebsocket:
    """Home Assistant websocket session with request/response by id."""

    def __init__(self, ha_url: str, token: str) -> None:
        self._ws = WebSocket(ha_url)
        self._ws.connect()
        self._id = 0
        try:
            hello = self._ws.recv_json()
            if hello.get("type") != "auth_required":
                raise SetupError(f"Onverwacht websocket-begin: {hello.get('type')}")
            self._ws.send_json({"type": "auth", "access_token": token})
            result = self._ws.recv_json()
            if result.get("type") != "auth_ok":
                raise SetupError(f"Websocket-login geweigerd: {result.get('message', result.get('type'))}")
        except BaseException:
            self._ws.close()
            raise

    def request(self, msg: dict) -> dict | list:
        self._id += 1
        self._ws.send_json({"id": self._id, **msg})
        while True:
            data = self._ws.recv_json()
            if data.get("id") == self._id and data.get("type") == "result":
                if not data.get("success"):
                    error = data.get("error") or {}
                    raise SetupError(
                        f"{msg['type']} mislukt: {error.get('code')}: {error.get('message')}"
                    )
                return data["result"]
            # events from other subscriptions are ignored

    def close(self) -> None:
        self._ws.close()


# ------------------------------------------------------------------- steps --


def step_api(ha_url: str, token: str) -> None:
    status, data = http_json("GET", f"{ha_url}/api/", token=token)
    if status == 401:
        raise SetupError(
            "Home Assistant weigert de token (HTTP 401). Maak een nieuwe "
            "long-lived token aan: Profiel -> Beveiliging."
        )
    # HA answers {"message": "API running."}
    if status != 200 or not isinstance(data, dict) or data.get("message") != "API running.":
        raise SetupError(f"Onverwacht antwoord van {ha_url}/api/ (HTTP {status}: {str(data)[:100]})")
    print(f"  [OK] Home Assistant bereikbaar op {ha_url}")


def find_mistral_entry(ha_url: str, token: str, ws: HaWebsocket) -> dict | None:
    status, entries = http_json(
        "GET", f"{ha_url}/api/config/config_entries/entry?domain={DOMAIN}", token=token
    )
    if status != 200 or not isinstance(entries, list):
        raise SetupError(f"Config entries opvragen mislukt (HTTP {status})")
    entries = [e for e in entries if e.get("domain") == DOMAIN]
    if not entries:
        return None
    # The API never returns entry data (it holds the API key), so the
    # provider can't be read from it. Only Mistral entries get an STT
    # entity; the default title is the fallback for an entry that never
    # finished loading.
    registry = ws.request({"type": "config/entity_registry/list"})
    stt_owners = {
        r.get("config_entry_id")
        for r in registry
        if r.get("platform") == DOMAIN and r.get("entity_id", "").startswith("stt.")
    }
    for match in (
        lambda e: e["entry_id"] in stt_owners,
        lambda e: e.get("title") == MISTRAL_TITLE,
    ):
        if found := next((e for e in entries if match(e)), None):
            return found
    others = ", ".join(e.get("title", "?") for e in entries)
    print(f"  [INFO] Geen Mistral-entry gevonden (wel: {others}); ik maak er een aan.")
    return None


def step_entry(
    ha_url: str, token: str, ws: HaWebsocket, get_api_key: Callable[[], str]
) -> str:
    if entry := find_mistral_entry(ha_url, token, ws):
        print(
            f"  [OK] Bestaande Mistral-entry hergebruikt: {entry.get('title')} "
            f"({entry['entry_id'][:8]}..., status: {entry.get('state')})"
        )
        return entry["entry_id"]

    api_key = get_api_key()
    status, flow = http_json(
        "POST", f"{ha_url}/api/config/config_entries/flow",
        token=token, body={"handler": DOMAIN},
    )
    if not is_flow(flow, "form", "user"):
        raise SetupError(
            f"Config flow kon niet starten ({describe(status, flow)}). "
            "Is de integratie via HACS geinstalleerd en Home Assistant herstart?"
        )
    flow_path = f"/api/config/config_entries/flow/{flow['flow_id']}"
    try:
        status, flow = http_json(
            "POST", f"{ha_url}{flow_path}", token=token, body={"provider": "mistral"}
        )
        if not is_flow(flow, "form", "api_key"):
            raise SetupError(f"Provider-stap mislukt ({describe(status, flow)})")
        status, flow = http_json(
            "POST", f"{ha_url}{flow_path}", token=token, body={"api_key": api_key}
        )
        if not is_flow(flow, "create_entry"):
            raise SetupError(f"API-key-stap mislukt ({describe(status, flow)})")
    except SetupError:
        abort_flow(ha_url, token, flow_path)
        raise
    print(f"  [OK] Config entry aangemaakt: {flow['title']}")
    return flow["result"]["entry_id"]


def current_form_values(flow: dict) -> dict:
    """Current option values, as HA pre-fills them in the options form."""
    values = {}
    for field in flow.get("data_schema") or []:
        suggested = (field.get("description") or {}).get("suggested_value")
        if suggested is not None:
            values[field["name"]] = suggested
    return values


def step_options(
    ha_url: str, token: str, entry_id: str, overrides: dict, hass_api: str | None
) -> dict:
    """Merge overrides into the current options and save them."""
    status, flow = http_json(
        "POST", f"{ha_url}/api/config/config_entries/options/flow",
        token=token, body={"handler": entry_id},
    )
    if not is_flow(flow, "form"):
        raise SetupError(f"Options flow kon niet starten ({describe(status, flow)})")
    flow_path = f"/api/config/config_entries/options/flow/{flow['flow_id']}"

    options = current_form_values(flow)
    options.update({k: v for k, v in overrides.items() if v is not None})
    if hass_api:
        apis = list(options.get("llm_hass_api") or [])
        if hass_api not in apis:
            apis.append(hass_api)
        options["llm_hass_api"] = apis

    status, result = http_json("POST", f"{ha_url}{flow_path}", token=token, body=options)
    if not is_flow(result, "create_entry"):
        abort_flow(ha_url, token, flow_path)
        raise SetupError(f"Opties opslaan mislukt ({describe(status, result)})")
    print("  [OK] Opties opgeslagen (de entry wordt herladen)")
    for key in ("llm_hass_api", "chat_model", "stt_model", "tts_model", "tts_voice_id"):
        if key in options:
            print(f"       {key}: {options[key]}")
    if not options.get("tts_voice_id"):
        print("  [LET OP] Geen voice-ID ingesteld: TTS geeft dan een fout. Zet MISTRAL_VOICE_ID en draai opnieuw.")
    return options


def _available(ha_url: str, token: str, entity_id: str) -> bool:
    status, state = http_json("GET", f"{ha_url}/api/states/{entity_id}", token=token)
    return status == 200 and isinstance(state, dict) and state.get("state") != "unavailable"


def step_wait_entities(
    ha_url: str, token: str, ws: HaWebsocket, entry_id: str, timeout: float = ENTITY_TIMEOUT
) -> dict[str, str]:
    """Wait until the entry's conversation/stt/tts entities are available."""
    deadline = time.monotonic() + timeout
    while True:
        found: dict[str, str] = {}
        for item in ws.request({"type": "config/entity_registry/list"}):
            if item.get("config_entry_id") != entry_id or item.get("disabled_by"):
                continue
            domain = item["entity_id"].partition(".")[0]
            if domain in WANTED_DOMAINS:
                found.setdefault(domain, item["entity_id"])
        ready = len(found) == len(WANTED_DOMAINS) and all(
            _available(ha_url, token, eid) for eid in found.values()
        )
        if ready:
            for domain in WANTED_DOMAINS:
                print(f"  [OK] {domain}-entiteit: {found[domain]}")
            return found
        if time.monotonic() > deadline:
            missing = [d for d in WANTED_DOMAINS if d not in found] or [
                f"{eid} (unavailable)" for eid in found.values()
            ]
            raise SetupError(
                f"Entiteiten niet klaar binnen {timeout:.0f} s (ontbrekend: "
                f"{', '.join(missing)}). Kijk bij Instellingen -> Apparaten & "
                "diensten -> LLM Conversation of de entry geladen is."
            )
        time.sleep(2)


def step_pipeline(
    ws: HaWebsocket,
    entities: dict[str, str],
    pipeline_name: str,
    language: str,
    make_preferred: bool = True,
) -> str:
    listing = ws.request({"type": "assist_pipeline/pipeline/list"})
    pipelines = listing.get("pipelines", [])
    preferred = listing.get("preferred_pipeline")
    target = next(
        (p for p in pipelines if p.get("name", "").casefold() == pipeline_name.casefold()),
        None,
    )
    fields = {
        "conversation_engine": entities["conversation"],
        # '*' like HA does for LLM agents: intent fallback then uses the
        # (more specific) STT/TTS language.
        "conversation_language": "*",
        "language": language.split("-")[0],
        "stt_engine": entities["stt"],
        "stt_language": language,
        "tts_engine": entities["tts"],
        "tts_language": language,
        "tts_voice": None,  # the voice is an entry option, not per pipeline
    }
    if target is None:
        created = ws.request({
            "type": "assist_pipeline/pipeline/create",
            "name": pipeline_name,
            "wake_word_entity": None,
            "wake_word_id": None,
            # Simple commands ("zet de lamp aan") are handled locally:
            # faster and free; everything else goes to Mistral.
            "prefer_local_intents": True,
            **fields,
        })
        pipeline_id = created["id"]
        print(f"  [OK] Nieuwe pipeline '{pipeline_name}' aangemaakt (Voxtral STT -> Mistral -> Voxtral TTS)")
    else:
        # update replaces the whole pipeline: carry over what we don't set
        update = {
            "type": "assist_pipeline/pipeline/update",
            "pipeline_id": target["id"],
            "name": target["name"],
            "wake_word_entity": target.get("wake_word_entity"),
            "wake_word_id": target.get("wake_word_id"),
            **fields,
        }
        for key in ("prefer_local_intents", "acknowledge_media_id"):
            if target.get(key) is not None:
                update[key] = target[key]
        ws.request(update)
        pipeline_id = target["id"]
        print(f"  [OK] Pipeline '{target['name']}' bijgewerkt (Voxtral STT -> Mistral -> Voxtral TTS)")

    if make_preferred and pipeline_id != preferred:
        ws.request({"type": "assist_pipeline/pipeline/set_preferred", "pipeline_id": pipeline_id})
        previous = next((p["name"] for p in pipelines if p["id"] == preferred), None)
        note = f" (was '{previous}'; die pipeline blijft ongewijzigd bestaan)" if previous else ""
        print(f"  [OK] '{pipeline_name}' is nu de voorkeurspipeline{note}")
    elif pipeline_id == preferred:
        print(f"  [OK] '{pipeline_name}' was al de voorkeurspipeline")
    return pipeline_id


# -------------------------------------------------------------------- main --


def read_secret(env: str, label: str) -> str:
    """Secret from the environment, or asked with hidden input."""
    if value := os.environ.get(env, "").strip():
        return value
    if not sys.stdin.isatty():
        raise SetupError(f"{env} is niet gezet en er is geen terminal om erom te vragen.")
    value = getpass.getpass(f"{label} (verborgen invoer): ").strip()
    if not value:
        raise SetupError(f"Geen {label} opgegeven.")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--ha-url", default=os.environ.get("HASS_URL"),
                        help="bijv. http://homeassistant.local:8123 (of env HASS_URL)")
    parser.add_argument("--voice-id", default=os.environ.get("MISTRAL_VOICE_ID"),
                        help="voice-ID uit Mistral Studio (of env MISTRAL_VOICE_ID); standaard de huidige waarde")
    parser.add_argument("--stt-model", default=None,
                        help=f"standaard de huidige waarde, bij een nieuwe entry {DEFAULT_STT_MODEL}")
    parser.add_argument("--tts-model", default=None,
                        help=f"standaard de huidige waarde, bij een nieuwe entry {DEFAULT_TTS_MODEL}")
    parser.add_argument("--hass-api", default="assist",
                        help="HA LLM API voor apparaatbesturing (standaard 'assist'; '' = niet aanpassen)")
    parser.add_argument("--pipeline-name", default=DEFAULT_PIPELINE_NAME,
                        help="pipeline die wordt aangemaakt of bijgewerkt (standaard 'Mistral')")
    parser.add_argument("--no-preferred", action="store_true",
                        help="maak de pipeline niet de voorkeurspipeline")
    parser.add_argument("--language", default=DEFAULT_LANGUAGE,
                        help="taal voor STT/TTS (standaard nl-NL; pipeline-taal wordt 'nl')")
    args = parser.parse_args()

    ha_url = (args.ha_url or "").strip().rstrip("/")
    if not ha_url:
        print("Geef de Home Assistant-URL op via HASS_URL of --ha-url.")
        return 2

    print("=== LLM Conversation: automatische installatie ===\n")
    try:
        token = read_secret("HASS_TOKEN", "Home Assistant long-lived token")

        print("[1/5] Home Assistant API")
        step_api(ha_url, token)

        ws = HaWebsocket(ha_url, token)
        try:
            print("[2/5] Config entry (provider: Mistral AI)")
            entry_id = step_entry(
                ha_url, token, ws, lambda: read_secret("MISTRAL_API_KEY", "Mistral API-key")
            )

            print("[3/5] Opties (apparaatbesturing + Voxtral-modellen + stem)")
            step_options(
                ha_url, token, entry_id,
                {
                    "stt_model": args.stt_model,
                    "tts_model": args.tts_model,
                    "tts_voice_id": args.voice_id,
                },
                args.hass_api,
            )

            print("[4/5] Wachten op de entiteiten")
            entities = step_wait_entities(ha_url, token, ws, entry_id)

            print("[5/5] Assist-pipeline")
            step_pipeline(ws, entities, args.pipeline_name, args.language, not args.no_preferred)
        finally:
            ws.close()
    except SetupError as err:
        print(f"\n[FOUT] {err}")
        return 1
    except (ConnectionError, OSError) as err:
        print(f"\n[FOUT] Verbinding met Home Assistant mislukt: {err}")
        return 1
    except KeyboardInterrupt:
        print("\nAfgebroken.")
        return 130

    print("\nKlaar! Vraag Assist iets, bijvoorbeeld: \"Welke lampen staan aan?\"")
    print("Valideer daarna met: python3 scripts/test_realtime.py (Windows: py scripts\\test_realtime.py)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
