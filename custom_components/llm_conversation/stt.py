"""The stt platform for the LLM Conversation integration (Mistral Voxtral)."""

from __future__ import annotations

from collections.abc import AsyncIterable
import struct
from typing import Any

from homeassistant.components import stt
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import LLMConversationConfigEntry
from .const import (
    CONF_STT_MODEL,
    DEFAULT_STT_MODEL,
    DOMAIN,
    PROVIDER_LABELS,
    STT_LANGUAGES,
)
from .providers import ProviderError


def _int_value(value: Any) -> int:
    """Return an int for plain values and enums alike."""
    return int(getattr(value, "value", value))


def _build_wav(data: bytes, metadata: stt.SpeechMetadata) -> bytes:
    """Wrap raw PCM chunks in a WAV container for the Mistral API."""
    sample_rate = _int_value(metadata.sample_rate)
    bit_rate = _int_value(metadata.bit_rate)
    channels = _int_value(metadata.channel)
    block_align = channels * bit_rate // 8
    byte_rate = sample_rate * block_align
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(data),
        b"WAVE",
        b"fmt ",
        16,  # PCM chunk size
        1,  # audio format: PCM
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bit_rate,
        b"data",
        len(data),
    )
    return header + data


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: LLMConversationConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Voxtral speech-to-text entity."""
    async_add_entities([MistralVoxtralSTTEntity(config_entry)])


class MistralVoxtralSTTEntity(stt.SpeechToTextEntity):
    """Speech-to-text entity backed by Mistral's Voxtral API."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Speech to text"

    def __init__(self, entry: LLMConversationConfigEntry) -> None:
        """Initialize the entity."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}-stt"
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer=PROVIDER_LABELS[entry.data["provider"]],
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    @property
    def supported_languages(self) -> list[str]:
        """Return the list of supported languages."""
        return STT_LANGUAGES

    @property
    def supported_formats(self) -> list[stt.AudioFormats]:
        """Return the list of supported formats."""
        return [stt.AudioFormats.WAV]

    @property
    def supported_codecs(self) -> list[stt.AudioCodecs]:
        """Return the list of supported codecs."""
        return [stt.AudioCodecs.PCM]

    @property
    def supported_bit_rates(self) -> list[stt.AudioBitRates]:
        """Return the list of supported bit rates."""
        return [stt.AudioBitRates.BITRATE_16]

    @property
    def supported_sample_rates(self) -> list[stt.AudioSampleRates]:
        """Return the list of supported sample rates."""
        return [stt.AudioSampleRates.SAMPLE_RATE_16000]

    @property
    def supported_channels(self) -> list[stt.AudioChannels]:
        """Return the list of supported channels."""
        return [stt.AudioChannels.CHANNEL_1]

    @callback
    def check_metadata(self, metadata: stt.SpeechMetadata) -> bool:
        """Validate metadata.

        Voxtral handles many languages and audio formats, so this entity
        accepts everything the Assist pipeline sends and never blocks.
        """
        return True

    async def async_process_audio_stream(
        self, metadata: stt.SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> stt.SpeechResult:
        """Transcribe the audio stream with a Voxtral model."""
        chunks = [chunk async for chunk in stream]
        wav = _build_wav(b"".join(chunks), metadata)
        client = self._entry.runtime_data
        model = self._entry.options.get(CONF_STT_MODEL, DEFAULT_STT_MODEL)
        try:
            text = await client.async_transcribe(wav, model)
        except ProviderError as err:
            raise HomeAssistantError(
                f"Voxtral transcription failed: {err}"
            ) from err
        return stt.SpeechResult(text=text, result=stt.SpeechResultState.SUCCESS)
