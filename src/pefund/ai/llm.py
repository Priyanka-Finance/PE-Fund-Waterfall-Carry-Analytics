"""Provider-agnostic language-model clients.

Three back-ends share one tiny interface (``complete(system, user) -> str``):

* ``anthropic`` - Claude through the official SDK (``ANTHROPIC_API_KEY``);
* ``openai``    - any OpenAI-compatible chat endpoint (``OPENAI_API_KEY`` and,
  for other hosts such as a local Ollama server, ``OPENAI_BASE_URL``);
* ``offline``   - no model at all; the deterministic writer in
  :mod:`pefund.ai.narrator` produces the commentary.

Nothing here is required to run the project: with no key set, everything falls
back to ``offline``.  Requests send only the model name, the prompts and a
token limit, so they work across model generations.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

#: Claude model used when ``PEFUND_LLM_MODEL`` is not set.
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5-5"
PROVIDERS = ("anthropic", "openai", "offline")


class LLMError(RuntimeError):
    """Raised when a language-model provider cannot be used."""


class LLMClient(Protocol):
    provider: str
    model: str

    def complete(self, system: str, user: str, max_tokens: int = 2500) -> str: ...


@dataclass
class AnthropicClient:
    model: str = DEFAULT_ANTHROPIC_MODEL
    api_key: str | None = None
    provider: str = "anthropic"

    def complete(self, system: str, user: str, max_tokens: int = 2500) -> str:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise LLMError("Install the Anthropic SDK first: pip install anthropic") from exc
        client = anthropic.Anthropic(api_key=self.api_key or os.environ.get("ANTHROPIC_API_KEY"))
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except Exception as exc:  # network, authentication, unknown model ...
            raise LLMError(f"Anthropic request failed: {exc}") from exc
        # A reply can hold several content blocks; only text blocks carry the answer.
        parts = [block.text for block in response.content if getattr(block, "type", "") == "text"]
        return "".join(parts).strip()


@dataclass
class OpenAICompatibleClient:
    model: str
    api_key: str | None = None
    base_url: str | None = None
    provider: str = "openai"

    def complete(self, system: str, user: str, max_tokens: int = 2500) -> str:
        try:
            import openai
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise LLMError("Install the OpenAI SDK first: pip install openai") from exc
        client = openai.OpenAI(
            api_key=self.api_key or os.environ.get("OPENAI_API_KEY") or "not-needed",
            base_url=self.base_url or os.environ.get("OPENAI_BASE_URL") or None,
        )
        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            )
        except Exception as exc:
            raise LLMError(f"OpenAI-compatible request failed: {exc}") from exc
        return (response.choices[0].message.content or "").strip()


def detect_provider() -> str:
    """Provider implied by the environment: an explicit choice, else whichever key is set."""
    chosen = (os.environ.get("PEFUND_LLM_PROVIDER") or "").strip().lower()
    if chosen:
        if chosen not in PROVIDERS:
            raise LLMError(f"PEFUND_LLM_PROVIDER must be one of {PROVIDERS}, got '{chosen}'")
        return chosen
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_BASE_URL"):
        return "openai"
    return "offline"


def get_client(provider: str | None = None, model: str | None = None) -> LLMClient | None:
    """Return a client for ``provider`` (auto-detected when omitted), or ``None`` for offline."""
    provider = (provider or detect_provider()).lower()
    model = model or os.environ.get("PEFUND_LLM_MODEL") or None
    if provider == "offline":
        return None
    if provider == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise LLMError("ANTHROPIC_API_KEY is not set")
        return AnthropicClient(model=model or DEFAULT_ANTHROPIC_MODEL)
    if provider == "openai":
        if not model:
            raise LLMError(
                "Set PEFUND_LLM_MODEL (or pass --model) to a chat model your OpenAI-compatible provider offers"
            )
        return OpenAICompatibleClient(model=model)
    raise LLMError(f"Unknown provider '{provider}'. Choose from {PROVIDERS}")
