"""Provider options that depend on the model, kept in one place."""

from __future__ import annotations

from app.helpers.config import get_settings


def reasoning_params(model: str | None) -> dict[str, str]:
    """``reasoning_effort`` for the gpt-oss models when ``GROQ_REASONING_EFFORT`` is set, else nothing.

    gpt-oss models think before they answer and those hidden tokens count against ``max_tokens`` and
    the provider's tokens-per-minute quota. A lower effort answers sooner and cheaper. Nothing is sent
    unless the setting is configured, and never to a model that does not take the option.
    """
    effort = get_settings().GROQ_REASONING_EFFORT
    if effort and str(model or "").startswith("openai/gpt-oss"):
        return {"reasoning_effort": effort}
    return {}
