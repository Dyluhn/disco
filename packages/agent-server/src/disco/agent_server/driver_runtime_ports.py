"""Concrete narrow ports for driver ownership wiring."""

from __future__ import annotations

from typing import Protocol

from disco.tools import AppKitPhase, DefaultToolExecutor

from .driver_context_state import DriverContextState
from .driver_runtime import LiveModelProbe


class DriverSettingsState(Protocol):
    def _get_model_override(self, conversation_id: str) -> str | None: ...

    def assist_override(self, conversation_id: str) -> bool | None: ...


class DriverExecutorLookup(Protocol):
    def executor_for(self, conversation_id: str) -> DefaultToolExecutor | None: ...


class RuntimeDriverSelections:
    """Bridge settings and context owners to one driver selection."""

    def __init__(
        self,
        settings: DriverSettingsState,
        contexts: DriverContextState,
    ) -> None:
        self._settings = settings
        self._contexts = contexts

    def model_override(self, conversation_id: str) -> str | None:
        return self._settings._get_model_override(conversation_id)

    def assist_override(self, conversation_id: str) -> bool | None:
        return self._settings.assist_override(conversation_id)

    def compose_model_key(self, conversation_id: str) -> str | None:
        return self._contexts.compose_model_key(conversation_id)


class RuntimeDriverPromptState:
    """Late-bound prompt mode over the executor owner."""

    def __init__(self, executors: DriverExecutorLookup) -> None:
        self._executors = executors

    def workflow_router_enabled(self) -> bool:
        from . import runtime as runtime_module

        return runtime_module.workflow_router_enabled()

    def workflow_router_active(self, conversation_id: str) -> bool:
        executor = self._executors.executor_for(conversation_id)
        scope = getattr(executor, "_scope", None)
        return getattr(scope, "preset", None) == "workflow_router"

    def appkit_mode_active(self, conversation_id: str) -> bool:
        executor = self._executors.executor_for(conversation_id)
        phase_state = getattr(executor, "appkit_phase", None)
        return getattr(phase_state, "phase", None) != AppKitPhase.CUSTOM_BUILD


class RuntimeDriverProbeSeams:
    """Resolve runtime module probe seams at call time."""

    def probe_live_model(
        self,
        base_url: str | None,
        api_key: str | None = None,
        model_id: str | None = None,
    ) -> LiveModelProbe:
        from . import runtime as runtime_module

        return runtime_module._probe_live_model(base_url, api_key, model_id)

    def do_live_model_probe(
        self,
        base_url: str,
        api_key: str | None,
        model_id: str | None = None,
    ) -> LiveModelProbe:
        from . import runtime as runtime_module

        return runtime_module._do_live_model_probe(base_url, api_key, model_id)

    def model_label(self, model_id: str) -> str:
        from . import runtime as runtime_module

        return runtime_module._model_label(model_id)
