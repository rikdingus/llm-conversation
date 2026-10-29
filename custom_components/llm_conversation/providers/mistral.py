"""Client for the Mistral API."""

from __future__ import annotations

import base64
from typing import Any

import aiohttp

from . import (
    TRANSCRIBE_TIMEOUT,
    ProviderError,
    raise_for_status,
)
from .openai_compat import OpenAICompatibleClient

MISTRAL_BASE_URL = "https://api.mistral.ai/v1"


class MistralClient(OpenAICompatibleClient):
    """Client for the Mistral chat, transcription and speech APIs."""

    def __init__(self, session, api_key: str) -> None:
        """Initialize the client."""
        super().__init__(session, api_key, MISTRAL_BASE_URL)

    @property
    def _auth_headers(self) -> dict[str, str]:
        """Return headers for requests where aiohttp sets the content type."""
        return {"Authorization": f"Bearer {self._api_key}"}

    async def async_transcribe(self, audio: bytes, model: str) -> str:
        """Transcribe a WAV file with a Voxtral transcription model."""
        form = aiohttp.FormData()
        form.add_field("model", model)
        form.add_field(
            "file", audio, filename="audio.wav", content_type="audio/wav"
        )
        async with self._session.post(
            f"{self._base_url}/audio/transcriptions",
            headers=self._auth_headers,
            data=form,
            timeout=aiohttp.ClientTimeout(total=TRANSCRIBE_TIMEOUT),
        ) as resp:
            await raise_for_status(resp)
            data = await resp.json()
            return data.get("text", "")

    async def async_speech(
        self, text: str, model: str, voice_id: str | None = None
    ) -> bytes:
        """Generate speech from text with a Voxtral TTS model."""
        payload: dict[str, Any] = {
            "model": model,
            "input": text,
            "response_format": "mp3",
        }
        if voice_id:
            payload["voice_id"] = voice_id
        async with self._session.post(
            f"{self._base_url}/audio/speech",
            headers=self._headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=TRANSCRIBE_TIMEOUT),
        ) as resp:
            await raise_for_status(resp)
            data = await resp.json()
            audio_data = data.get("audio_data")
            if not audio_data:
                raise ProviderError("TTS response contained no audio data")
            return base64.b64decode(audio_data)
