"""The tts platform for the LLM Conversation integration (Mistral Voxtral)."""

from __future__ import annotations

from homeassistant.components import tts
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import LLMConversationConfigEntry
from .const import (
    CONF_TTS_MODEL,
    CONF_TTS_VOICE_ID,
    DEFAULT_TTS_MODEL,
    DOMAIN,
    PROVIDER_LABELS,
    TTS_LANGUAGES,
)
from .providers import ProviderError


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: LLMConversationConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Voxtral text-to-speech entity."""
    async_add_entities([MistralVoxtralTTSEntity(config_entry)])


class MistralVoxtralTTSEntity(tts.TextToSpeechEntity):
    """Text-to-speech entity backed by Mistral's Voxtral API."""

    _attr_has_entity_name = True
    _attr_name = "Text to speech"
    _attr_supported_languages = TTS_LANGUAGES
    _attr_default_language = "en"

    def __init__(self, entry: LLMConversationConfigEntry) -> None:
        """Initialize the entity."""
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}-tts"
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer=PROVIDER_LABELS[entry.data["provider"]],
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    async def async_get_tts_audio(
        self, message: str, language: str, options: dict
    ) -> tts.TtsAudioType:
        """Generate speech for the given text with a Voxtral TTS model."""
        client = self._entry.runtime_data
        model = self._entry.options.get(CONF_TTS_MODEL, DEFAULT_TTS_MODEL)
        voice_id = self._entry.options.get(CONF_TTS_VOICE_ID)
        try:
            audio = await client.async_speech(message, model, voice_id)
        except ProviderError as err:
            raise HomeAssistantError(
                f"Voxtral speech generation failed: {err}"
            ) from err
        return "mp3", audio
