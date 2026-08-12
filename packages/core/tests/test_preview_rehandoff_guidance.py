"""Recovery guidance for a managed Preview generation rotation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from disco.core import DeliverableEvent
from disco.core.loop.boundaries import HostVerificationDeliverable
from disco.core.loop.finish.verify_gate_parts import host_disposition
from disco.core.loop.finish.verify_gate_parts.host_disposition import (
    _governed_non_pass_guidance,
)


def _guidance(*, rehandoff: bool, summary: str = "managed-preview authority is unavailable") -> str:
    return _governed_non_pass_guidance(
        typed_result=None,
        host_label="unavailable",
        next_action="",
        summary=summary,
        failed_claim=None,
        unavailable_claim=None,
        preview_rehandoff_required=rehandoff,
    )


def test_generation_rotation_requires_new_handoff_not_preview_restart() -> None:
    guidance = _guidance(rehandoff=True)

    assert "call `serve` again for the same app artifact" in guidance
    assert "then call `finish` again" in guidance
    assert "Do not restart a healthy preview" in guidance
    assert "Start a managed preview" not in guidance


def test_missing_preview_keeps_existing_start_guidance() -> None:
    guidance = _guidance(rehandoff=False)

    assert "Start a managed preview" in guidance
    assert "call `serve` again" not in guidance


def test_rotation_reported_during_verification_also_requires_rehandoff() -> None:
    guidance = _guidance(
        rehandoff=False,
        summary="selected preview generation is absent, changed, or foreign",
    )

    assert "call `serve` again for the same app artifact" in guidance
    assert "Start a managed preview" not in guidance


def test_rehandoff_predicate_requires_two_different_real_preview_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handoff = DeliverableEvent(title="App", path="index.html", artifact_kind="app").model_copy(
        update={"seq": 3}
    )
    deliverable = HostVerificationDeliverable(
        conversation_id="conv-preview",
        artifact_path="index.html",
        artifact_kind="app",
    )
    monkeypatch.setattr(host_disposition, "_latest_app_deliverable_event", lambda _events: handoff)
    selections = iter(
        [
            cast(Any, SimpleNamespace(operational_identity=("preview-old",))),
            cast(Any, SimpleNamespace(operational_identity=("preview-current",))),
        ]
    )
    monkeypatch.setattr(
        host_disposition,
        "preview_selection_at",
        lambda *_args, **_kwargs: next(selections),
    )

    assert host_disposition._preview_rehandoff_required([handoff], deliverable)
