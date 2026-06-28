"""CXT-1 tests: ContextPack.from_ledger derivation — resolved-failure exclusion,
per-kind capping, never-compact exemption, anchor passthrough, roundtrip."""

from __future__ import annotations

from disco.core.context import (
    ArtifactMemoryKind,
    ArtifactMemoryRef,
    CompactionPolicy,
    ContextLedger,
    ContextPack,
    DirectEditKind,
    DirectEditRef,
    ResourceRef,
    Severity,
    VerifierFailureRef,
)


def _failure(i: int, *, resolved: bool = False) -> VerifierFailureRef:
    return VerifierFailureRef(kind="console_error", message=f"err {i}", resolved=resolved, severity=Severity.ERROR)


def _resource(i: int) -> ResourceRef:
    return ResourceRef(rel_path=f"assets/img{i}.png", source=f"upload://{i}")


def test_from_ledger_passes_through_anchors() -> None:
    todo = ArtifactMemoryRef(kind=ArtifactMemoryKind.TODO, rel_path=".disco/context/todo.md")
    led = ContextLedger.empty("c").model_copy(
        update={"active_goal": "goal", "active_contract": "static.site", "current_version": 3, "todo_ref": todo}
    )
    pack = ContextPack.from_ledger(led, allowed_next_actions=("edit", "verify"), todo_text="- [ ] do thing")
    assert pack.active_goal == "goal"
    assert pack.active_contract == "static.site"
    assert pack.current_version == 3
    assert pack.todo_ref == todo
    assert pack.current_todo == "- [ ] do thing"
    assert pack.allowed_next_actions == ("edit", "verify")


def test_from_ledger_excludes_resolved_failures() -> None:
    led = ContextLedger.empty("c").model_copy(
        update={
            "latest_verifier_failures": (
                _failure(1, resolved=False),
                _failure(2, resolved=True),
                _failure(3, resolved=False),
            )
        }
    )
    pack = ContextPack.from_ledger(led)
    msgs = {f.message for f in pack.latest_failures}
    assert msgs == {"err 1", "err 3"}
    assert all(not f.resolved for f in pack.latest_failures)


def test_from_ledger_caps_omittable_but_exempts_never_compact() -> None:
    # tiny caps to force the distinction; resources are omittable, failures and
    # direct-edits are never-compact.
    pol = CompactionPolicy(max_resource_refs=2, max_recoverable_refs=2, max_comments=2)
    led = ContextLedger.empty("c").model_copy(
        update={
            "resource_manifest": tuple(_resource(i) for i in range(5)),
            "latest_verifier_failures": tuple(_failure(i) for i in range(5)),
            "direct_edits": tuple(
                DirectEditRef(target_id=f"t{i}", rel_path="index.html", kind=DirectEditKind.TEXT)
                for i in range(5)
            ),
            "unresolved_comments": tuple(f"c{i}" for i in range(5)),
            "retained_refs": tuple(
                ArtifactMemoryRef(kind=ArtifactMemoryKind.SUMMARY, rel_path=f".disco/context/sum{i}.md")
                for i in range(5)
            ),
        }
    )
    pack = ContextPack.from_ledger(led, policy=pol)
    # omittable kinds capped to the policy limit
    assert len(pack.resource_refs) == 2
    assert len(pack.unresolved_comments) == 2
    assert len(pack.recoverable_refs) == 2
    # never-compact kinds kept in FULL despite tiny caps
    assert len(pack.latest_failures) == 5
    assert len(pack.direct_edits_summary) == 5


def test_from_ledger_default_policy_used_when_none() -> None:
    # 40 resources > default cap (30) → capped to default
    led = ContextLedger.empty("c").model_copy(
        update={"resource_manifest": tuple(_resource(i) for i in range(40))}
    )
    pack = ContextPack.from_ledger(led)
    assert len(pack.resource_refs) == CompactionPolicy.default().max_resource_refs == 30


def test_context_pack_roundtrip() -> None:
    led = ContextLedger.empty("c").model_copy(
        update={"active_goal": "g", "latest_verifier_failures": (_failure(1),)}
    )
    pack = ContextPack.from_ledger(led)
    assert ContextPack.model_validate(pack.model_dump(mode="json")) == pack
