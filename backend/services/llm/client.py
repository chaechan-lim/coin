"""Multi-provider LLM client with fallback chain and retry logic."""
from __future__ import annotations

import asyncio
from typing import Any

import structlog

from services.llm.providers import (
    LLMResponse,
    ToolCall,
    AnthropicProvider,
    GeminiProvider,
    LLMProvider,
)

logger = structlog.get_logger(__name__)

# Re-export for convenience
__all__ = ["LLMClient", "LLMResponse", "ToolCall"]


class LLMClient:
    """Provider-agnostic LLM client with automatic fallback.

    Fallback chain: primary model → fallback model → gemini model.
    Each model gets multiple retry attempts before moving to the next.
    """

    def __init__(self, config):
        """Initialize from LLMConfig.

        Parameters
        ----------
        config : LLMConfig
            Must have: model, fallback_model, api_key,
            Optional: gemini_api_key, gemini_fallback_model
        """
        self._config = config
        self._anthropic: AnthropicProvider | None = None
        self._gemini: GeminiProvider | None = None

        # Lazy-init providers
        if config.api_key:
            try:
                self._anthropic = AnthropicProvider(api_key=config.api_key)
            except Exception as e:
                logger.warning("anthropic_provider_init_failed", error=str(e))

        gemini_key = getattr(config, "gemini_api_key", "")
        if gemini_key:
            try:
                self._gemini = GeminiProvider(api_key=gemini_key)
            except Exception as e:
                logger.warning("gemini_provider_init_failed", error=str(e))

    def _resolve_provider(self, model: str) -> LLMProvider | None:
        if model.startswith("gemini"):
            return self._gemini
        return self._anthropic

    def _build_fallback_chain(self, model_override: str | None = None) -> list[tuple[str, LLMProvider]]:
        """Build [(model_name, provider), ...] in fallback order."""
        chain = []

        primary = model_override or self._config.model
        provider = self._resolve_provider(primary)
        if provider:
            chain.append((primary, provider))

        if self._config.fallback_model:
            p = self._resolve_provider(self._config.fallback_model)
            if p:
                chain.append((self._config.fallback_model, p))

        gemini_model = getattr(self._config, "gemini_fallback_model", "")
        if gemini_model and self._gemini:
            chain.append((gemini_model, self._gemini))

        return chain

    async def generate(
        self,
        messages: list[dict],
        *,
        max_tokens: int = 4096,
        system: str | None = None,
        model: str | None = None,
        retries: int = 3,
    ) -> LLMResponse:
        """Text generation with retry + fallback.

        Raises RuntimeError if all providers fail.
        """
        return await self._call_with_fallback(
            messages=messages,
            max_tokens=max_tokens,
            system=system,
            tools=None,
            model_override=model,
            retries=retries,
        )

    async def generate_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        max_tokens: int = 2048,
        system: str | None = None,
        model: str | None = None,
        retries: int = 2,
    ) -> LLMResponse:
        """Tool-use generation with retry + fallback.

        Raises RuntimeError if all providers fail.
        """
        return await self._call_with_fallback(
            messages=messages,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            model_override=model,
            retries=retries,
        )

    def format_tool_loop_messages(
        self,
        response: LLMResponse,
        tool_results: list[dict],
    ) -> tuple[dict, dict]:
        """Build (assistant_msg, user_msg) for multi-turn tool loop.

        Delegates to the provider that produced the response.
        """
        provider = self._resolve_provider(response.model)
        if not provider:
            raise RuntimeError(f"No provider for model: {response.model}")
        return provider.format_tool_loop_messages(response, tool_results)

    async def _call_with_fallback(
        self,
        messages: list[dict],
        max_tokens: int,
        system: str | None,
        tools: list[dict] | None,
        model_override: str | None,
        retries: int,
    ) -> LLMResponse:
        chain = self._build_fallback_chain(model_override)
        if not chain:
            raise RuntimeError("No LLM providers configured")

        last_error: Exception | None = None

        for model, provider in chain:
            for attempt in range(retries):
                try:
                    response = await provider.create(
                        messages=messages,
                        model=model,
                        max_tokens=max_tokens,
                        system=system,
                        tools=tools,
                    )
                    logger.debug(
                        "llm_call_success",
                        model=model,
                        attempt=attempt + 1,
                        has_tools=bool(tools),
                    )
                    return response
                except Exception as e:
                    last_error = e
                    wait = 2 ** attempt * 2  # 2s, 4s, 8s
                    logger.warning(
                        "llm_call_failed",
                        model=model,
                        attempt=attempt + 1,
                        error=str(e)[:200],
                        retry_in=wait if attempt < retries - 1 else None,
                    )
                    if attempt < retries - 1:
                        await asyncio.sleep(wait)

            logger.warning("llm_model_exhausted", model=model)

        raise RuntimeError(f"All LLM providers failed: {last_error}")
