"""Bounded Build Soak scenario io owner."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

from .common import (
    _BATCH_SUMMARY_NAME,
    _SCENARIOS,
    _TRIGGER_AFTER_FIRST_FILE_WRITE,
)


def batch_summary_name(requested: str | None) -> str:
    """Validate a batch-report filename. A bare, safe filename only.

    Rejecting separators matters: a name like ``../batch-summary.json`` would put
    a promotion-visible report back into a parent tree, quietly undoing the
    structural exclusion a non-promoting lane is relying on.
    """

    if requested is None or requested == "":
        return _BATCH_SUMMARY_NAME
    if "/" in requested or "\\" in requested or requested in {".", ".."}:
        raise ValueError(f"batch summary name must be a bare filename, got {requested!r}")
    if not requested.endswith(".json"):
        raise ValueError(f"batch summary name must end in .json, got {requested!r}")
    return requested


def load_scenarios(path: str | Path = _SCENARIOS) -> dict[str, dict[str, Any]]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    scenarios = raw.get("scenarios") or []
    defaults = raw.get("defaults") or {}

    def merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        out = copy.deepcopy(base)
        for key, value in override.items():
            # A governed verification policy is one exact admission contract,
            # not a bag of defaults. Inheriting web claim floors into an AppKit
            # or future device policy would silently ask the wrong verifier.
            if key == "governed_verification":
                out[key] = copy.deepcopy(value)
            elif isinstance(value, dict) and isinstance(out.get(key), dict):
                out[key] = merge(out[key], value)
            else:
                out[key] = copy.deepcopy(value)
        return out

    loaded = [merge(defaults, s) for s in scenarios if s.get("id")]
    return {str(s["id"]): s for s in loaded}


def _materialize_task_seed(value: Any, seed: int) -> Any:
    """Replace the frozen ``{{seed}}`` token throughout one scenario copy."""
    if isinstance(value, str):
        return value.replace("{{seed}}", str(seed))
    if isinstance(value, list):
        return [_materialize_task_seed(item, seed) for item in value]
    if isinstance(value, dict):
        return {key: _materialize_task_seed(item, seed) for key, item in value.items()}
    return copy.deepcopy(value)


def _driver_catalog_contains(payload: dict[str, Any], model: str) -> bool:
    """Whether the live agent reports the exact saved key as driver-eligible."""

    models = payload.get("models")
    if not isinstance(models, list):
        return False
    return any(
        isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"] == model
        for entry in models
    )


class ContextWindowPinError(RuntimeError):
    """A scenario's declared driver context-window pin was not the window the run used.

    Raised instead of returning an unpinned measurement: the product resolves an
    unknown ``model_override`` by silently falling back to the configured default
    driver (``driver_runtime.context_window``), so a missing or misconfigured
    catalogue entry would otherwise yield a run that LOOKS pinned and is not.
    """

    def __init__(self, reason: str, facts: dict[str, Any]) -> None:
        super().__init__(f"{reason}: {facts}")
        self.reason = reason
        self.facts = facts


def _driver_context_window_pin(scenario: dict[str, Any]) -> tuple[str, int] | None:
    """The scenario's declared driver context-window pin, or None.

    Shape (both keys required)::

        driver_context_window:
          model: <catalogue key>   # the per-conversation model_override to send
          window: <positive int>   # the window that key MUST advertise

    Two keys rather than one because the product exposes no per-conversation
    context-window field: the only per-conversation driver selector is the
    catalogue key (``CreateConversationBody.model_override``), and the window
    lives on that key's ``ModelEntry.context_window``. ``model`` selects; ``window``
    is the claim the run's own spans are checked against. A malformed declaration
    raises rather than being ignored — silently dropping the pin would produce
    exactly the unpressured measurement the pin exists to prevent.
    """
    declared = scenario.get("driver_context_window")
    if declared is None:
        return None
    if not isinstance(declared, dict):
        raise ValueError("driver_context_window must be an object with 'model' and 'window'")
    model = declared.get("model")
    window = declared.get("window")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("driver_context_window.model must be a nonempty catalogue key")
    # bool is an int subclass — exclude it explicitly.
    if type(window) is not int or window <= 0:
        raise ValueError("driver_context_window.window must be a positive int")
    return model.strip(), window


def _observed_driver_context_windows(inspect_trace: dict[str, Any]) -> list[int]:
    """Every ``driver_context_window`` the run's own ``agent.step`` spans recorded.

    The loop stamps the resolved window on each driver call
    (``agent.py``'s ``log_span("agent.step", …, driver_context_window=…)``), so the
    trace is the run's own record of the window it actually budgeted against —
    stronger evidence than the catalogue, which only says what was configured.
    """
    spans = inspect_trace.get("spans")
    if not isinstance(spans, list):
        return []
    observed: list[int] = []
    for span in spans:
        if not isinstance(span, dict) or span.get("span") != "agent.step":
            continue
        value = span.get("driver_context_window")
        if type(value) is int:
            observed.append(value)
    return observed


def _verify_driver_context_window_pin(
    scenario: dict[str, Any],
    inspect_trace: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Prove from the run's own spans that a declared pin is the window it used.

    Returns the recorded facts, or None when the scenario declares no pin. Raises
    ``ContextWindowPinError`` when the pin is declared and the trace contradicts
    it. A run with NO driver calls is not an error here: it made no measurement to
    be fooled by, and any assertion that needed the pin fails on its own terms.
    """
    pin = _driver_context_window_pin(scenario)
    if pin is None:
        return None
    model, window = pin
    if inspect_trace is None:
        raise ContextWindowPinError(
            "driver context-window pin cannot be proven without an inspect trace",
            {"model": model, "declared_window": window},
        )
    observed = _observed_driver_context_windows(inspect_trace)
    distinct = sorted(set(observed))
    facts = {
        "model": model,
        "declared_window": window,
        "steps_observed": len(observed),
        "distinct_windows": distinct,
    }
    if observed and distinct != [window]:
        raise ContextWindowPinError("driver context-window pin was not honoured", facts)
    return facts


def _declared_workspace_paths(scenario: dict[str, Any]) -> list[str]:
    files = ((scenario.get("assertions") or {}).get("workspace") or {}).get("files") or []
    return [str(f["path"]) for f in files if f.get("path")]


def _preview_required(scenario: dict[str, Any]) -> bool:
    return bool(((scenario.get("assertions") or {}).get("preview") or {}).get("required"))


def _browser_verification_required(scenario: dict[str, Any]) -> bool:
    assertion = (scenario.get("assertions") or {}).get("browser_verification") or {}
    return isinstance(assertion, dict) and assertion.get("required") is True


def _is_cancel_after_first_write(cancel_at: dict[str, Any] | None) -> bool:
    return (
        isinstance(cancel_at, dict) and cancel_at.get("trigger") == _TRIGGER_AFTER_FIRST_FILE_WRITE
    )
