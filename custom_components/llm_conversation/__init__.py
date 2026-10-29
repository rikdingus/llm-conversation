"""The LLM Conversation integration."""

from __future__ import annotations

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_PROVIDER,
    DOMAIN,
    PROVIDER_ANTHROPIC,
    PROVIDER_GITHUB_MODELS,
    PROVIDER_GOOGLE,
    PROVIDER_MISTRAL,
)
from .providers import (
    ProviderAuthError,
    ProviderClient,
    ProviderConnectionError,
    ProviderError,
)
from .providers.anthropic import AnthropicClient
from .providers.github_models import GitHubModelsClient
from .providers.google import GeminiClient
from .providers.mistral import MistralClient

PLATFORMS = [Platform.CONVERSATION]
PLATFORMS_SPEECH = [Platform.STT, Platform.TTS]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

CLIENT_CLASSES: dict[str, type[ProviderClient]] = {
    PROVIDER_MISTRAL: MistralClient,
    PROVIDER_ANTHROPIC: AnthropicClient,
    PROVIDER_GOOGLE: GeminiClient,
    PROVIDER_GITHUB_MODELS: GitHubModelsClient,
}

type LLMConversationConfigEntry = ConfigEntry[ProviderClient]


def _platforms(entry: LLMConversationConfigEntry) -> list[Platform]:
    """Return the platforms for an entry (speech only for Mistral)."""
    if entry.data[CONF_PROVIDER] == PROVIDER_MISTRAL:
        return PLATFORMS + PLATFORMS_SPEECH
    return PLATFORMS


async def async_setup_entry(
    hass: HomeAssistant, entry: LLMConversationConfigEntry
) -> bool:
    """Set up LLM Conversation from a config entry."""
    client_class = CLIENT_CLASSES[entry.data[CONF_PROVIDER]]
    client = client_class(async_get_clientsession(hass), entry.data[CONF_API_KEY])

    try:
        await client.async_list_models()
    except ProviderAuthError as err:
        raise ConfigEntryAuthFailed(err) from err
    except (ProviderConnectionError, aiohttp.ClientError, TimeoutError) as err:
        # Network not up yet (e.g. right after a power cut): let HA retry
        # instead of leaving the entry in a permanent setup error.
        raise ConfigEntryNotReady(err) from err
    except ProviderError as err:
        raise ConfigEntryNotReady(err) from err

    entry.runtime_data = client

    await hass.config_entries.async_forward_entry_setups(entry, _platforms(entry))

    entry.async_on_unload(entry.add_update_listener(async_update_options))

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: LLMConversationConfigEntry
) -> bool:
    """Unload a LLM Conversation config entry."""
    return await hass.config_entries.async_unload_platforms(
        entry, _platforms(entry)
    )


async def async_update_options(
    hass: HomeAssistant, entry: LLMConversationConfigEntry
) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)
