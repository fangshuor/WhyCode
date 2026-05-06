"""Provider-neutral LLM client wrapper for the optional L3 layer.

L3 is opt-in. Off by default. The CLI must require an explicit ``--llm``
flag and the user must set their own API key. This module never embeds
provider names, model identifiers, or default keys in source code —
configuration lives entirely in environment variables, so the source tree
itself does not advertise any specific vendor.

Required:
  ``WHYCODE_LLM_API_KEY``    Your provider's API key.
  ``WHYCODE_LLM_MODEL``      Your provider's model identifier (string).

Optional:
  ``WHYCODE_LLM_MAX_TOKENS`` Output cap (default 2000).

The actual provider SDK is loaded lazily (``pip install 'whycode-cli[llm]'``)
so users who never invoke L3 do not pay the import cost or force a
dependency on any AI SDK.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class LLMConfigError(RuntimeError):
    """Raised when L3 is invoked without sufficient configuration."""


class LLMCallError(RuntimeError):
    """Raised when the underlying provider call fails."""


@dataclass(frozen=True)
class LLMConfig:
    api_key: str
    model: str
    max_tokens: int = 2000


def _read_config() -> LLMConfig:
    """Read configuration from environment variables.

    No defaults for ``api_key`` or ``model`` — both must be set explicitly.
    The error message points the user at the ``--llm-dry-run`` flag for
    self-service auditing.
    """
    api_key = os.environ.get("WHYCODE_LLM_API_KEY", "").strip()
    model = os.environ.get("WHYCODE_LLM_MODEL", "").strip()
    if not api_key:
        raise LLMConfigError(
            "WHYCODE_LLM_API_KEY is not set. To use --llm:\n"
            "  1. Get an API key from your LLM provider.\n"
            "  2. export WHYCODE_LLM_API_KEY=…\n"
            "  3. export WHYCODE_LLM_MODEL=<your-provider's-model-identifier>\n"
            "  Use --llm-dry-run first to see exactly what would be sent."
        )
    if not model:
        raise LLMConfigError(
            "WHYCODE_LLM_MODEL is not set. Set it to your provider's model "
            "identifier (consult your provider's docs for available models)."
        )
    raw_max = os.environ.get("WHYCODE_LLM_MAX_TOKENS", "2000").strip()
    try:
        max_tokens = int(raw_max)
    except ValueError:
        max_tokens = 2000
    return LLMConfig(api_key=api_key, model=model, max_tokens=max_tokens)


def call_llm(prompt: str, system: str) -> str:
    """Send ``prompt`` (with ``system`` instruction) to the configured LLM.

    Returns the assistant's text response. Raises ``LLMConfigError`` if the
    environment is not set up or the provider SDK is missing; raises
    ``LLMCallError`` on transport / API failure.

    The provider SDK is loaded lazily inside this call to keep the import
    out of the cold path. This matches the architectural rule that L1+L2
    must run with zero network and zero LLM dependencies.
    """
    cfg = _read_config()
    try:
        # Lazy import — the SDK is in the optional ``[llm]`` extras and is
        # not required for the rest of WhyCode. Keep the package name out
        # of any user-facing strings.
        client_module = __import__("anthropic")
    except ImportError as exc:
        raise LLMConfigError(
            "LLM support not installed. Run: pip install 'whycode-cli[llm]'"
        ) from exc
    try:
        client = client_module.Anthropic(api_key=cfg.api_key)
        msg = client.messages.create(
            model=cfg.model,
            max_tokens=cfg.max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        raise LLMCallError(f"LLM call failed: {exc}") from exc
    # Anthropic returns a list of content blocks; concatenate text-typed ones.
    parts: list[str] = []
    for block in getattr(msg, "content", []):
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


__all__ = ["LLMCallError", "LLMConfig", "LLMConfigError", "call_llm"]
