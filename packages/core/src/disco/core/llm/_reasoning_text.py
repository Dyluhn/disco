"""Normalize textual reasoning fields on compatible chat responses."""

from collections.abc import Mapping


def reasoning_text(message: Mapping[str, object]) -> str:
    """Read either wire spelling without consulting endpoint or model identity.

    Some endpoints mirror a delta under both keys. Prefer the established
    spelling when nonempty so a mirrored token is never counted twice. Empty
    or malformed values must neither hide the other spelling nor count as
    model progress. Structured reasoning metadata is not answer text.
    """
    for field in ("reasoning_content", "reasoning"):
        value = message.get(field)
        if isinstance(value, str) and value:
            return value
    return ""
