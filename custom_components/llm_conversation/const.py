"""Constants for the LLM Conversation integration."""

import logging
from typing import Final

DOMAIN: Final = "llm_conversation"
LOGGER = logging.getLogger(__package__)

CONF_CHAT_MODEL: Final = "chat_model"
CONF_MAX_TOKENS: Final = "max_tokens"
CONF_TEMPERATURE: Final = "temperature"
CONF_PROVIDER: Final = "provider"
CONF_STT_MODEL: Final = "stt_model"
CONF_TTS_MODEL: Final = "tts_model"
CONF_TTS_VOICE_ID: Final = "tts_voice_id"

RECOMMENDED_TEMPERATURE: Final = 0.2
RECOMMENDED_MAX_TOKENS: Final = 1024

# Max number of back and forth with the LLM (tool calls) allowed
# to generate a single response.
MAX_TOOL_ITERATIONS: Final = 8

PROVIDER_MISTRAL: Final = "mistral"
PROVIDER_ANTHROPIC: Final = "anthropic"
PROVIDER_GOOGLE: Final = "google"
PROVIDER_GITHUB_MODELS: Final = "github_models"

PROVIDER_LABELS: Final = {
    PROVIDER_MISTRAL: "Mistral AI",
    PROVIDER_ANTHROPIC: "Anthropic Claude",
    PROVIDER_GOOGLE: "Google Gemini",
    PROVIDER_GITHUB_MODELS: "GitHub Models (Copilot)",
}

# Current model IDs (October 2026). Every field also accepts any other
# model ID via the custom value option in the model dropdown.
CHAT_MODELS: Final = {
    PROVIDER_MISTRAL: [
        "mistral-small-2603",
        "mistral-medium-3-5",
        "mistral-large-2512",
        "mistral-large-4",
        "ministral-8b-2512",
        "ministral-14b-2512",
    ],
    PROVIDER_ANTHROPIC: [
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
        "claude-fable-5-1",
    ],
    PROVIDER_GOOGLE: [
        "gemini-4-argon",
        "gemini-3.8-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
    ],
    PROVIDER_GITHUB_MODELS: [
        "openai/gpt-4.1",
        "openai/gpt-4o",
        "openai/gpt-4o-mini",
    ],
}

DEFAULT_CHAT_MODELS: Final = {
    PROVIDER_MISTRAL: "mistral-small-2603",
    PROVIDER_ANTHROPIC: "claude-sonnet-5-5",
    PROVIDER_GOOGLE: "gemini-3.8-flash",
    PROVIDER_GITHUB_MODELS: "openai/gpt-4.1",
}

# Current Claude models reject non-default sampling parameters, so the
# Anthropic client does not send a temperature at all.
PROVIDERS_SUPPORTING_TEMPERATURE: Final = {
    PROVIDER_MISTRAL,
    PROVIDER_GOOGLE,
    PROVIDER_GITHUB_MODELS,
}

# Voxtral speech models (October 2026). Transcription via
# /v1/audio/transcriptions, speech generation via /v1/audio/speech.
STT_MODELS: Final = [
    "voxtral-mini-latest",
    "voxtral-small-transcribe-3-0",
    "voxtral-mini-transcribe-3-0",
    "voxtral-mini-2602",
]
DEFAULT_STT_MODEL: Final = "voxtral-mini-latest"

TTS_MODELS: Final = [
    "voxtral-mini-tts-3-0",
    "voxtral-mini-tts-2603",
]
DEFAULT_TTS_MODEL: Final = "voxtral-mini-tts-3-0"

# Languages reported by the STT entity. Voxtral covers these and more;
# check_metadata is overridden so a mismatch never blocks a pipeline.
STT_LANGUAGES: Final = [
    "en",
    "en-US",
    "en-GB",
    "nl",
    "nl-NL",
    "fr",
    "fr-FR",
    "de",
    "de-DE",
    "es",
    "es-ES",
    "it",
    "it-IT",
    "pt",
    "pt-PT",
    "pt-BR",
    "ru",
    "ru-RU",
    "ar",
    "hi",
    "zh",
    "zh-CN",
    "ja",
    "ja-JP",
    "ko",
    "ko-KR",
]

# Languages reported by the TTS entity (Voxtral TTS is multilingual).
TTS_LANGUAGES: Final = [
    "en",
    "en-US",
    "en-GB",
    "nl",
    "nl-NL",
    "fr",
    "fr-FR",
    "de",
    "de-DE",
    "es",
    "es-ES",
    "it",
    "it-IT",
    "pt",
    "pt-PT",
    "pt-BR",
    "pl",
    "ar",
    "ru",
    "ru-RU",
    "tr",
    "sv",
    "da",
    "fi",
    "no",
    "cs",
    "ro",
    "hu",
    "el",
    "he",
    "uk",
    "id",
    "ms",
    "vi",
    "th",
    "hi",
    "bn",
    "ja",
    "ja-JP",
    "ko",
    "ko-KR",
    "zh",
    "zh-CN",
    "zh-TW",
]
