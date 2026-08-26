"""Small pure helpers for driver runtime diagnostics."""

from disco.core.llm import LLMError


def _with_detail(exc: LLMError) -> str:
    """Append a sanitized provider account/configuration reason."""
    detail = getattr(exc, "provider_detail", "")
    return f"{exc} — {detail}" if detail else str(exc)
