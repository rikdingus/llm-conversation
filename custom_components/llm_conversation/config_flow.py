"""Config flow for the LLM Conversation integration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import aiohttp
import probatio

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import (
    CONF_API_KEY,
    CONF_LLM_HASS_API,
    CONF_PROMPT,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import llm
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TemplateSelector,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import CLIENT_CLASSES, LLMConversationConfigEntry
from .const import (
    CHAT_MODELS,
    CONF_CHAT_MODEL,
    CONF_MAX_TOKENS,
    CONF_PROVIDER,
    CONF_STT_MODEL,
    CONF_TEMPERATURE,
    CONF_TTS_MODEL,
    CONF_TTS_VOICE_ID,
    DEFAULT_CHAT_MODELS,
    DEFAULT_STT_MODEL,
    DEFAULT_TTS_MODEL,
    DOMAIN,
    PROVIDER_LABELS,
    PROVIDER_MISTRAL,
    PROVIDERS_SUPPORTING_TEMPERATURE,
    RECOMMENDED_MAX_TOKENS,
    RECOMMENDED_TEMPERATURE,
    STT_MODELS,
    TTS_MODELS,
)
from .providers import ProviderAuthError, ProviderConnectionError, ProviderError

STEP_PROVIDER_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_PROVIDER): SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(label=label, value=provider_id)
                    for provider_id, label in PROVIDER_LABELS.items()
                ],
                mode=SelectSelectorMode.LIST,
            )
        ),
    },
)

STEP_API_KEY_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_API_KEY): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    },
)


async def validate_input(hass: HomeAssistant, provider: str, api_key: str) -> None:
    """Validate that the API key allows us to connect."""
    client = CLIENT_CLASSES[provider](async_get_clientsession(hass), api_key)
    await client.async_list_models()


class LLMConversationConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the config flow for LLM Conversation."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._provider: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the provider selection step."""
        if user_input is None:
            return self.async_show_form(
                step_id="user", data_schema=STEP_PROVIDER_SCHEMA
            )
        self._provider = user_input[CONF_PROVIDER]
        return await self.async_step_api_key()

    async def async_step_api_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the API key step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            provider = self._provider
            assert provider is not None
            try:
                await validate_input(self.hass, provider, user_input[CONF_API_KEY])
            except ProviderAuthError:
                errors["base"] = "invalid_auth"
            except (ProviderConnectionError, aiohttp.ClientError, TimeoutError):
                errors["base"] = "cannot_connect"
            except ProviderError:
                errors["base"] = "unknown"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
            else:
                # Multiple entries are allowed: one agent per entry,
                # e.g. a fast agent for voice and a large agent for chat.
                return self.async_create_entry(
                    title=PROVIDER_LABELS[provider],
                    data={
                        CONF_PROVIDER: provider,
                        CONF_API_KEY: user_input[CONF_API_KEY],
                    },
                )

        return self.async_show_form(
            step_id="api_key",
            data_schema=self.add_suggested_values_to_schema(
                STEP_API_KEY_SCHEMA, user_input
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Perform reauth upon an API authentication error."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Dialog that informs the user that reauth is required."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()
        provider = reauth_entry.data[CONF_PROVIDER]

        if user_input is not None:
            try:
                await validate_input(self.hass, provider, user_input[CONF_API_KEY])
            except ProviderAuthError:
                errors["base"] = "invalid_auth"
            except (ProviderConnectionError, aiohttp.ClientError, TimeoutError):
                errors["base"] = "cannot_connect"
            except ProviderError:
                errors["base"] = "unknown"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data={**reauth_entry.data, CONF_API_KEY: user_input[CONF_API_KEY]},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                STEP_API_KEY_SCHEMA, user_input
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: LLMConversationConfigEntry,
    ) -> LLMConversationOptionsFlowHandler:
        """Return the options flow."""
        return LLMConversationOptionsFlowHandler()


class LLMConversationOptionsFlowHandler(OptionsFlow):
    """Handle the options flow for LLM Conversation."""

    async def async_step_init(  # noqa: C901
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            if user_input.get(CONF_LLM_HASS_API) is None:
                user_input.pop(CONF_LLM_HASS_API, None)
            return self.async_create_entry(title="", data=user_input)

        entry: LLMConversationConfigEntry = self.config_entry
        provider = entry.data[CONF_PROVIDER]
        options = dict(entry.options)
        models = await self._async_get_models(entry, provider)

        schema: dict[Any, Any] = {
            probatio.Optional(
                CONF_PROMPT,
                description={
                    "suggested_value": options.get(
                        CONF_PROMPT, llm.DEFAULT_INSTRUCTIONS_PROMPT
                    )
                },
            ): TemplateSelector(),
            probatio.Optional(
                CONF_LLM_HASS_API,
                description={"suggested_value": options.get(CONF_LLM_HASS_API)},
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(label=api.name, value=api.id)
                        for api in llm.async_get_apis(self.hass)
                    ],
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            ),
            probatio.Optional(
                CONF_CHAT_MODEL,
                description={
                    "suggested_value": options.get(
                        CONF_CHAT_MODEL, DEFAULT_CHAT_MODELS[provider]
                    )
                },
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(label=model, value=model)
                        for model in models
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                    custom_value=True,
                )
            ),
            probatio.Optional(
                CONF_MAX_TOKENS,
                description={
                    "suggested_value": options.get(
                        CONF_MAX_TOKENS, RECOMMENDED_MAX_TOKENS
                    )
                },
            ): int,
        }

        if provider in PROVIDERS_SUPPORTING_TEMPERATURE:
            schema[
                probatio.Optional(
                    CONF_TEMPERATURE,
                    description={
                        "suggested_value": options.get(
                            CONF_TEMPERATURE, RECOMMENDED_TEMPERATURE
                        )
                    },
                )
            ] = NumberSelector(
                NumberSelectorConfig(min=0.0, max=1.0, step=0.05)
            )

        if provider == PROVIDER_MISTRAL:
            schema[
                probatio.Optional(
                    CONF_STT_MODEL,
                    description={
                        "suggested_value": options.get(
                            CONF_STT_MODEL, DEFAULT_STT_MODEL
                        )
                    },
                )
            ] = SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(label=model, value=model)
                        for model in STT_MODELS
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                    custom_value=True,
                )
            )
            schema[
                probatio.Optional(
                    CONF_TTS_MODEL,
                    description={
                        "suggested_value": options.get(
                            CONF_TTS_MODEL, DEFAULT_TTS_MODEL
                        )
                    },
                )
            ] = SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(label=model, value=model)
                        for model in TTS_MODELS
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                    custom_value=True,
                )
            )
            schema[
                probatio.Optional(
                    CONF_TTS_VOICE_ID,
                    description={
                        "suggested_value": options.get(CONF_TTS_VOICE_ID, "")
                    },
                )
            ] = TextSelector(
                TextSelectorConfig(type=TextSelectorType.TEXT)
            )

        return self.async_show_form(
            step_id="init",
            data_schema=probatio.Schema(schema),
        )

    @staticmethod
    async def _async_get_models(
        entry: LLMConversationConfigEntry, provider: str
    ) -> list[str]:
        """Fetch the model list from the provider, with a static fallback."""
        models = list(CHAT_MODELS.get(provider, []))
        client = getattr(entry, "runtime_data", None)
        if client is not None:
            try:
                fetched = await client.async_list_models()
            except (ProviderError, aiohttp.ClientError, TimeoutError):
                pass
            else:
                if fetched:
                    models = list(dict.fromkeys([*fetched, *models]))
        return models
