"""Request budget preview — a SOFT, tokenizer-free heuristic that triggers
the existing soft compaction ladder BEFORE a provider call. It never rejects
a call on its own; the typed provider context error remains authority."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .provider_ledger import ProviderRequestShape


@dataclass(frozen=True)
class RequestBudgetEstimate:
    """Scalar-only aggregate budget estimate for one would-be provider request.

    Every field is a plain scalar — no prompt strings, tool names, schemas,
    URLs, metadata, exceptions, or arbitrary dicts.
    """

    driver_context_window: int
    max_output_tokens: int | None
    canonical_payload_bytes: int
    messages_json_bytes: int
    tools_json_bytes: int
    message_count: int
    tool_count: int

    def __post_init__(self) -> None:
        if type(self.driver_context_window) is not int or self.driver_context_window <= 0:
            raise ValueError(
                f"driver_context_window must be a strictly positive int, "
                f"got {self.driver_context_window!r}"
            )
        if self.max_output_tokens is not None and (
            type(self.max_output_tokens) is not int or self.max_output_tokens <= 0
        ):
            raise ValueError(
                f"max_output_tokens must be strictly positive int or None, "
                f"got {self.max_output_tokens!r}"
            )
        if type(self.canonical_payload_bytes) is not int or self.canonical_payload_bytes <= 0:
            raise ValueError(
                f"canonical_payload_bytes must be a strictly positive int, "
                f"got {self.canonical_payload_bytes!r}"
            )

        def _nonneg(name: str, value: int) -> None:
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a nonnegative int, got {value!r}")

        _nonneg("messages_json_bytes", self.messages_json_bytes)
        _nonneg("tools_json_bytes", self.tools_json_bytes)
        _nonneg("message_count", self.message_count)
        _nonneg("tool_count", self.tool_count)

        if self.canonical_payload_bytes < self.messages_json_bytes + self.tools_json_bytes:
            raise ValueError(
                f"canonical_payload_bytes ({self.canonical_payload_bytes}) must be "
                f">= messages_json_bytes ({self.messages_json_bytes}) + "
                f"tools_json_bytes ({self.tools_json_bytes}) = "
                f"{self.messages_json_bytes + self.tools_json_bytes}"
            )

    @property
    def estimated_input_tokens(self) -> int:
        """Conservative ceil of canonical bytes / 3.25 → (bytes * 4 + 12) // 13.

        Exact integer arithmetic — no floating-point — so the result is
        deterministic across all platforms."""
        return (self.canonical_payload_bytes * 4 + 12) // 13

    @property
    def pressure_tokens(self) -> int:
        """Tokens that would need to fit in the context window.

        When max_output_tokens is present, adds it to estimated input so the
        model's output headroom is reserved. When absent, equals estimated
        input alone — the existing 65%/80% condenser thresholds already
        preserve output headroom for the summarizer path.
        """
        base = self.estimated_input_tokens
        if self.max_output_tokens is not None:
            return base + self.max_output_tokens
        return base


def preview_request_budget(
    shape: Callable[[], ProviderRequestShape | None],
) -> RequestBudgetEstimate | None:
    """Evaluate a side-effect-free wire shape; preview failure never blocks a call."""
    try:
        value = shape()
        if value is None or value.driver_context_window is None:
            return None
        return RequestBudgetEstimate(
            driver_context_window=value.driver_context_window,
            max_output_tokens=value.max_output_tokens,
            canonical_payload_bytes=value.canonical_payload_bytes,
            messages_json_bytes=value.messages_json_bytes,
            tools_json_bytes=value.tools_json_bytes,
            message_count=value.message_count,
            tool_count=value.tool_count,
        )
    except Exception:
        return None
