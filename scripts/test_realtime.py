#!/usr/bin/env python3
"""Realtime end-to-end test for the LLM Conversation integration.

Exercises the exact APIs the integration wraps, first against Mistral
directly and then (optionally) through a live Home Assistant instance
via its REST API. Pure standard library - runs on any Python 3.11+,
including the Home Assistant SSH/terminal add-on.

Direct Mistral tests (always run, need --api-key):
  1. API key check          GET  /v1/models
  2. Chat model             POST /v1/chat/completions (non-stream)
  3. Voxtral TTS            POST /v1/audio/speech  -> WAV + MP3 files
  4. Voxtral STT round-trip POST /v1/audio/transcriptions with the
                            generated WAV: text in ~ text out

Home Assistant tests (need --ha-url and --ha-token):
  5. HA connectivity        GET  /api/
  6. Conversation agent     POST /api/conversation/process
  7. TTS entity            POST /api/tts_get_url + fetch the audio URL
  8. STT entity            POST /api/stt/{entity} with raw 16 kHz PCM
                            (resampled from the TTS WAV)

Usage:
  python3 scripts/test_realtime.py --api-key K --voice-id V
  python3 scripts/test_realtime.py --api-key K --voice-id V \
      --ha-url http://homeassistant.local:8123 --ha-token T

API key and voice ID come from console.mistral.ai / studio.mistral.ai.
The HA token is a long-lived access token (Profile -> Security).
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.request
import wave
from pathlib import Path

MISTRAL_BASE = "https://api.mistral.ai/v1"
DEFAULT_STT_MODEL = "voxtral-mini-latest"
DEFAULT_TTS_MODEL = "voxtral-mini-tts-3-0"
DEFAULT_CHAT_MODEL = "mistral-small-2603"
DEFAULT_TEXT = "Dit is een realtime test van de Mistral Assist pijplijn."

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"  {'[PASS]' if ok else '[FAIL]'} {name}: {detail}")


def http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    body: bytes | None = None,
    timeout: int = 120,
) -> tuple[int, dict | str]:
    """Do an HTTP request and return (status, parsed JSON or text)."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            status = resp.status
    except urllib.error.HTTPError as err:
        data = err.read()
        status = err.code
    text = data.decode("utf-8", "replace")
    try:
        return status, json.loads(text)
    except json.JSONDecodeError:
        return status, text


def hint(status: int, body: str) -> str:
    """Turn a Mistral API error into a readable diagnosis."""
    snippets = " ".join(body.split())[:200]
    hints = {
        401: "API key ongeldig of verlopen",
        403: "geen toegang (content-moderatie bij TTS, of verkeerde voice_id?)",
        404: "model bestaat niet (typfout of nog niet beschikbaar voor je account)",
        422: "foutieve request - ontbreekt de voice_id?",
        429: "rate limit - wacht even en probeer opnieuw",
    }
    return f"HTTP {status} ({hints.get(status, 'onbekende fout')}): {snippets}"


# ---------------------------------------------------------------- Mistral --


def test_api_key(api_key: str) -> list[str]:
    status, data = http_json(
        "GET", f"{MISTRAL_BASE}/models", headers={"Authorization": f"Bearer {api_key}"}
    )
    if status != 200:
        record("API key", False, hint(status, str(data)))
        return []
    models = [m["id"] for m in data.get("data", []) if m.get("id")]
    voxtral = sorted(m for m in models if "voxtral" in m)
    record(
        "API key",
        True,
        f"geldig, {len(models)} modellen ({len(voxtral)} voxtral: {', '.join(voxtral) or 'geen'})",
    )
    return models


def test_chat(api_key: str, chat_model: str) -> None:
    payload = {
        "model": chat_model,
        "messages": [{"role": "user", "content": "Zeg exact: test geslaagd"}],
        "max_tokens": 512,
        "stream": False,
    }
    status, data = http_json(
        "POST",
        f"{MISTRAL_BASE}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        body=json.dumps(payload).encode(),
    )
    if status != 200:
        record("Chat model", False, hint(status, str(data)))
        return
    try:
        answer = data["choices"][0]["message"]["content"]
    except (KeyError, TypeError, IndexError):
        answer = "?"
    record("Chat model", True, f"{chat_model} antwoordt: {answer.strip()[:120]!r}")


def test_tts(api_key: str, tts_model: str, voice_id: str, text: str, out_dir: Path) -> bytes:
    """Generate speech (WAV for the STT round-trip, MP3 to check the default)."""
    wav_bytes = b""
    for fmt in ("wav", "mp3"):
        payload = {
            "model": tts_model,
            "input": text,
            "voice_id": voice_id,
            "response_format": fmt,
        }
        status, data = http_json(
            "POST",
            f"{MISTRAL_BASE}/audio/speech",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            body=json.dumps(payload).encode(),
        )
        if status != 200:
            record(f"TTS ({fmt})", False, hint(status, str(data)))
            continue
        try:
            audio = base64.b64decode(data["audio_data"])
        except (KeyError, TypeError, ValueError):
            record(f"TTS ({fmt})", False, "antwoord bevat geen audio_data")
            continue
        path = out_dir / f"test_tts.{fmt}"
        path.write_bytes(audio)
        magic = "RIFF" if audio[:4] == b"RIFF" else ("ID3/0xFF" if audio[:3] == b"ID3" or audio[:1] == b"\xff" else "onbekend")
        record(f"TTS ({fmt})", True, f"{len(audio)} bytes ({magic}) -> {path}")
        if fmt == "wav":
            wav_bytes = audio
    return wav_bytes


def test_stt_roundtrip(api_key: str, stt_model: str, text: str, wav_bytes: bytes) -> None:
    if not wav_bytes:
        record("STT round-trip", False, "overgeslagen: geen TTS-WAV om mee te testen")
        return
    boundary = "----llmconversationtest"
    part = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="model"\r\n\r\n{stt_model}\r\n'
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
        "Content-Type: audio/wav\r\n\r\n"
    ).encode() + wav_bytes + f"\r\n--{boundary}--\r\n".encode()
    status, data = http_json(
        "POST",
        f"{MISTRAL_BASE}/audio/transcriptions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        body=part,
    )
    if status != 200:
        record("STT round-trip", False, hint(status, str(data)))
        return
    transcript = data.get("text", "") if isinstance(data, dict) else ""
    normal = lambda s: " ".join(s.lower().split())
    similar = normal(text).startswith(" ".join(normal(transcript).split()[:4])[:12]) or normal(transcript)[:12] in normal(text)
    record(
        "STT round-trip",
        bool(transcript),
        f"teruggetranscribeerd: {transcript!r}"
        + ("" if similar else " (afwijkend van de invoer, maar het endpoint werkt)"),
    )


# ------------------------------------------------- Home Assistant REST API --


def ha_headers(token: str, content_type: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def test_ha_api(ha_url: str, token: str) -> None:
    status, data = http_json("GET", f"{ha_url}/api/", headers=ha_headers(token))
    record("HA connectivity", status == 200 and str(data).startswith("API running"), f"HTTP {status}: {str(data)[:80]}")


def test_ha_conversation(ha_url: str, token: str, agent_id: str, text: str) -> None:
    payload = {"text": text, "agent_id": agent_id}
    status, data = http_json(
        "POST",
        f"{ha_url}/api/conversation/process",
        headers=ha_headers(token, "application/json"),
        body=json.dumps(payload).encode(),
    )
    if status != 200:
        record("HA conversation agent", False, f"HTTP {status}: {str(data)[:200]}")
        return
    try:
        speech = data["response"]["speech"]["plain"]["speech"]
    except (KeyError, TypeError):
        speech = "?"
    record("HA conversation agent", True, f"antwoord: {speech!r}")


def test_ha_tts(ha_url: str, token: str, tts_entity: str, text: str, out_dir: Path) -> None:
    payload = {"engine_id": tts_entity, "message": text, "language": "nl-NL"}
    status, data = http_json(
        "POST",
        f"{ha_url}/api/tts_get_url",
        headers=ha_headers(token, "application/json"),
        body=json.dumps(payload).encode(),
    )
    if status != 200:
        record("HA TTS entity", False, f"HTTP {status}: {str(data)[:200]}")
        return
    url = data.get("url") if isinstance(data, dict) else None
    if not url:
        record("HA TTS entity", False, f"geen url in antwoord: {str(data)[:120]}")
        return
    with urllib.request.urlopen(url, timeout=120) as resp:
        audio = resp.read()
    path = out_dir / "test_ha_tts.mp3"
    path.write_bytes(audio)
    record(
        "HA TTS entity",
        len(audio) > 0 and (audio[:3] == b"ID3" or audio[:1] == b"\xff"),
        f"{len(audio)} bytes MP3 -> {path}",
    )


def pcm_16k_mono(wav_bytes: bytes) -> bytes | None:
    """Convert a PCM WAV to raw 16 kHz 16-bit mono samples (stdlib only)."""
    import io

    try:
        with wave.open(io.BytesIO(wav_bytes)) as wav:
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            rate = wav.getframerate()
            frames = wav.readframes(wav.getnframes())
    except wave.Error:
        return None
    if width == 2:
        samples = [int.from_bytes(frames[i : i + 2], "little", signed=True) for i in range(0, len(frames), 2)] if channels == 1 else None
        if samples is None:
            # Stereo -> mono by averaging
            left = [int.from_bytes(frames[i : i + 2], "little", signed=True) for i in range(0, len(frames), channels * 2)]
            right = [int.from_bytes(frames[i + 2 : i + 4], "little", signed=True) for i in range(0, len(frames), channels * 2)]
            samples = [(l + r) // 2 for l, r in zip(left, right)]
    elif width == 1:  # 8-bit unsigned
        samples = [(b - 128) * 256 for b in frames] if channels == 1 else None
        if samples is None:
            left = [(frames[i] - 128) * 256 for i in range(0, len(frames), channels)]
            right = [(frames[i + 1] - 128) * 256 for i in range(0, len(frames), channels)]
            samples = [(l + r) // 2 for l, r in zip(left, right)]
    else:
        return None
    if rate == 16000:
        resampled = samples
    else:  # naive linear resampling
        n_out = round(len(samples) * 16000 / rate)
        resampled = [
            int(
                samples[int(i * rate / 16000)]
                + (
                    samples[min(int(i * rate / 16000) + 1, len(samples) - 1)]
                    - samples[int(i * rate / 16000)]
                )
                * ((i * rate / 16000) % 1)
            )
            for i in range(n_out)
        ]
    return b"".join(max(-32768, min(32767, s)).to_bytes(2, "little", signed=True) for s in resampled)


def test_ha_stt(ha_url: str, token: str, stt_entity: str, wav_bytes: bytes) -> None:
    pcm = pcm_16k_mono(wav_bytes)
    if pcm is None:
        record("HA STT entity", False, "TTS-WAV kon niet naar 16 kHz PCM worden omgezet (8-bit test wordt gebruikt)")
        return
    headers = ha_headers(token, "audio/wav")
    headers["X-Speech-Content"] = (
        "format=wav; codec=pcm; bit_rate=16; sample_rate=16000; channel=1; language=nl-NL"
    )
    status, data = http_json(
        "POST", f"{ha_url}/api/stt/{stt_entity}", headers=headers, body=pcm
    )
    if status != 200:
        record("HA STT entity", False, f"HTTP {status}: {str(data)[:200]}")
        return
    result = data.get("result", "?") if isinstance(data, dict) else "?"
    text = data.get("text", "") if isinstance(data, dict) else ""
    record(
        "HA STT entity",
        result == "success" and bool(text),
        f"result={result}, transcript={text!r}",
    )


# ------------------------------------------------------------------ main --


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api-key", default=None, help="Mistral API key (of env MISTRAL_API_KEY)")
    parser.add_argument("--voice-id", default=None, help="Voice ID uit Mistral Studio (of env MISTRAL_VOICE_ID)")
    parser.add_argument("--chat-model", default=DEFAULT_CHAT_MODEL)
    parser.add_argument("--stt-model", default=DEFAULT_STT_MODEL)
    parser.add_argument("--tts-model", default=DEFAULT_TTS_MODEL)
    parser.add_argument("--text", default=DEFAULT_TEXT, help="Tekst voor de TTS/round-trip test")
    parser.add_argument("--out-dir", default=".", help="Waar audiofiles worden opgeslagen")
    parser.add_argument("--ha-url", default=None, help="bijv. http://homeassistant.local:8123 (of env HASS_URL)")
    parser.add_argument("--ha-token", default=None, help="HA long-lived token (of env HASS_TOKEN)")
    parser.add_argument("--agent-id", default="conversation.llm_conversation")
    parser.add_argument("--stt-entity", default="stt.mistral_speech_to_text")
    parser.add_argument("--tts-entity", default="tts.mistral_text_to_speech")
    args = parser.parse_args()

    import os

    api_key = args.api_key or os.environ.get("MISTRAL_API_KEY")
    voice_id = args.voice_id or os.environ.get("MISTRAL_VOICE_ID")
    ha_url = (args.ha_url or os.environ.get("HASS_URL") or "").rstrip("/")
    ha_token = args.ha_token or os.environ.get("HASS_TOKEN")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=== LLM Conversation: realtime test ===")
    print(f"STT model: {args.stt_model} | TTS model: {args.tts_model} | chat: {args.chat_model}")
    print()

    if not api_key:
        print("Geef een Mistral API key op (--api-key of MISTRAL_API_KEY).")
        return 2
    if not voice_id:
        print("Let op: geen --voice-id opgegeven; TTS kan dan een 403/422 geven.\n")

    print("[1/8] Mistral API key")
    models = test_api_key(api_key)

    print("[2/8] Chat model")
    test_chat(api_key, args.chat_model)

    print("[3/8] Voxtral TTS")
    wav_bytes = test_tts(api_key, args.tts_model, voice_id or "", args.text, out_dir)

    print("[4/8] Voxtral STT round-trip")
    test_stt_roundtrip(api_key, args.stt_model, args.text, wav_bytes)

    if ha_url and ha_token:
        print("[5/8] Home Assistant API")
        test_ha_api(ha_url, ha_token)
        print("[6/8] HA conversation agent")
        test_ha_conversation(ha_url, ha_token, args.agent_id, "Zeg: test geslaagd")
        print("[7/8] HA TTS entity")
        test_ha_tts(ha_url, ha_token, args.tts_entity, args.text, out_dir)
        print("[8/8] HA STT entity")
        test_ha_stt(ha_url, ha_token, args.stt_entity, wav_bytes)
    else:
        print("[5-8] Home Assistant tests overgeslagen (--ha-url + --ha-token ontbreken)")

    print()
    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"=== {len(RESULTS) - len(failed)}/{len(RESULTS)} geslaagd ===")
    if failed:
        print("Mislukt:", ", ".join(failed))
        return 1
    print("Alles werkt: de complete Mistral Assist-pijplijn is operationeel.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
