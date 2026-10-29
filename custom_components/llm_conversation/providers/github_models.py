"""Client for GitHub Models (the API behind GitHub Copilot models)."""

from typing import Any

import aiohttp

from . import DEFAULT_TIMEOUT, raise_for_status
from .openai_compat import OpenAICompatibleClient

GITHUB_MODELS_BASE_URL = "https://models.github.ai/inference"
GITHUB_MODELS_CATALOG_URL = "https://models.github.ai/catalog/models"


class GitHubModelsClient(OpenAICompatibleClient):
    """Client for the OpenAI-compatible GitHub Models inference API."""

    def __init__(self, session, api_key: str) -> None:
        """Initialize the client."""
        super().__init__(
            session,
            api_key,
            GITHUB_MODELS_BASE_URL,
            extra_headers={"Accept": "application/vnd.github+json"},
        )

    async def async_list_models(self) -> list[str]:
        """List available models from the GitHub Models catalog."""
        async with self._session.get(
            GITHUB_MODELS_CATALOG_URL,
            headers=self._headers,
            timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
        ) as resp:
            await raise_for_status(resp)
            data: Any = await resp.json()
            models = data if isinstance(data, list) else data.get("models", [])
            return [model["id"] for model in models if model.get("id")]
