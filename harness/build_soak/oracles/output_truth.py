"""OutputTruthOracle (guidelines §11.5, §16).

If the run says it FINISHED, the claimed output must actually exist (§7 truth
hierarchy: a model/agent saying "the build worked" never overrides workspace /
preview truth).

Checks (only when the run reached a finished/verified terminal state AND the
scenario asserts output):
  * required workspace files exist             -> FALSE_FINISH_NO_OUTPUT
  * required file contents present (must_contain) -> ARTIFACT_TRUTH_MISMATCH
  * preview health passes (when required)      -> FALSE_FINISH_PREVIEW_BROKEN
  * preview content present (must_contain)     -> PREVIEW_TRUTH_MISMATCH

Evidence shapes (captured by the runner; supplied as plain dicts):
  workspace_manifest: {"<relpath>": "<file content>", ...}
                  or  {"<relpath>": {"content": "..."}, ...}
                  or  {"files": {"<relpath>": "...", ...}}
  preview: {"health": {"status": 200|404|...}, "content": "<served html>"}
"""

from __future__ import annotations

from typing import Any

from disco.core.observations import scan_for_destructive_elision

from .. import failure_codes as fc
from ..events import terminal_status
from .schema import OracleResult, failing, passing, skipping

_ORACLE = "OutputTruthOracle"

_FINISHED_STATES = frozenset({"FINISHED", "VERIFIED"})

# A content mismatch on a file captured on one of these NON-AUTHORITATIVE bases (the snapshot
# readiness gate accepted it via extended content-stability, with NO file_write raw-sha and no
# qualifying full readback) is UNRELIABLE — the capture may be a stale pre-flush copy. Any
# OTHER proof level (raw_sha / rendered_readback / absent) OR an UNANNOTATED entry (a legacy /
# proxy capture with no `proof` key — treated as authoritative so we never silently downgrade)
# is PROVEN and a mismatch on it stays a hard ARTIFACT_TRUTH_MISMATCH.
_NON_AUTHORITATIVE_PROOF = frozenset({"unproven_extended_stability", "unknown"})


def _nested_files(manifest: dict[str, Any] | None) -> dict[str, Any]:
    if not manifest:
        return {}
    nested = manifest.get("files")
    return nested if isinstance(nested, dict) else manifest


def _normalize_workspace(manifest: dict[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for path, val in _nested_files(manifest).items():
        if isinstance(val, dict):
            out[str(path)] = str(val.get("content", ""))
        else:
            out[str(path)] = str(val)
    return out


def _proof_for(manifest: dict[str, Any] | None, path: str) -> str | None:
    """The recorded acceptance proof level for a declared path, or None when the entry is a
    plain string / carries no `proof` (legacy / proxy capture → treated as authoritative)."""
    files = _nested_files(manifest)
    entry = files.get(path)
    if entry is None:
        entry = files.get(path.lstrip("/"))
    if isinstance(entry, dict):
        proof = entry.get("proof")
        return str(proof) if proof is not None else None
    return None


def _fold_content_mismatches(mismatches: list[dict[str, Any]]):
    """DETERMINISTIC PRECEDENCE fold over ALL workspace-content mismatches in the run
    (Part A). A mismatch is AUTHORITATIVE unless its file's proof is non-authoritative
    (`unproven_extended_stability` / `unknown`). If ANY mismatch is authoritative → a hard
    ``ARTIFACT_TRUTH_MISMATCH`` (the proven regression WINS; never masked). ONLY if EVERY
    mismatch is non-authoritative → INVALID_RUN ``WORKSPACE_SNAPSHOT_UNVERIFIED`` (the capture
    is too uncertain to call a product failure). The full per-file evidence (proof + path +
    check class) rides in `facts["mismatches"]` either way."""
    proven = [m for m in mismatches if m.get("proof") not in _NON_AUTHORITATIVE_PROOF]
    first = mismatches[0]
    facts: dict[str, Any] = {
        "mismatches": mismatches,
        "proven_mismatch_count": len(proven),
        "unverified_mismatch_count": len(mismatches) - len(proven),
        # Back-compat top-level keys (the first mismatch) for existing triage/tests.
        "path": first.get("path"),
    }
    if "missing_substring" in first:
        facts["missing_substring"] = first["missing_substring"]
    if proven:
        return failing(
            _ORACLE,
            fc.ARTIFACT_TRUTH_MISMATCH,
            first_broken_link="finish -> required_content_present",
            facts=facts,
        )
    return failing(
        _ORACLE,
        fc.WORKSPACE_SNAPSHOT_UNVERIFIED,
        first_broken_link="finish -> snapshot_content_verifiable",
        facts=facts,
    )


class OutputTruthOracle:
    def check(
        self,
        events: list[dict[str, Any]],
        *,
        scenario: dict[str, Any] | None = None,
        workspace_manifest: dict[str, Any] | None = None,
        preview: dict[str, Any] | None = None,
    ) -> list[OracleResult]:
        assertions = (scenario or {}).get("assertions") or {}
        workspace_assert = assertions.get("workspace") or {}
        preview_assert = assertions.get("preview") or {}
        wants_output = bool(workspace_assert.get("files")) or bool(preview_assert.get("required"))

        if not wants_output:
            return [skipping(_ORACLE, reason="scenario asserts no output truth")]

        term = terminal_status(events)
        terminal_expected = (scenario or {}).get("assertions", {}).get("terminal_status_in")
        finished = term in _FINISHED_STATES or (
            bool(terminal_expected) and term in set(terminal_expected)
        )
        if not finished:
            # FAIL-CLOSED (migration 2026_06_24_paused_incomplete_not_pass): we only reach
            # here when the scenario REQUIRES output (wants_output) and/or a finished
            # terminal, but the run never reached one — e.g. the no-progress / actionless
            # valve PAUSED it, it timed out, or it ended at an unhandled gate. This is NOT a
            # "false finish" (nothing claimed finished) and it is NOT a PASS: a build that
            # does not complete is a failure even if some files happen to exist on disk.
            # Previously this SKIPPED, which let a paused-incomplete run score PASS — the
            # live-surfaced adjudicator gap (surfaced-bugs Bug 8 / conv_c0ff8684...).
            return [
                failing(
                    _ORACLE,
                    fc.BUILD_DID_NOT_FINISH,
                    first_broken_link="required_finish -> terminal_status",
                    facts={
                        "terminal_status": term,
                        "terminal_status_in": terminal_expected,
                        "reason": "run required to finish + deliver output but never reached "
                        "a finished/required terminal",
                    },
                )
            ]

        files = _normalize_workspace(workspace_manifest)

        # --- workspace file existence + content truth ---
        # Existence is judged FIRST and hard (a missing required deliverable is FALSE_FINISH,
        # never softened). Content checks (must_contain / must_not_contain / exact) across ALL
        # asserted files are COLLECTED with each file's capture proof level, then folded by
        # deterministic precedence (Part A): one PROVEN mismatch makes the whole run a hard
        # ARTIFACT_TRUTH_MISMATCH; all-non-authoritative → INVALID_RUN snapshot-unverified.
        content_mismatches: list[dict[str, Any]] = []
        for spec in workspace_assert.get("files") or []:
            path = spec.get("path")
            if path is None:
                continue
            if path not in files:
                return [
                    failing(
                        _ORACLE,
                        fc.FALSE_FINISH_NO_OUTPUT,
                        first_broken_link="finish -> required_file_exists",
                        facts={"required_path": path, "present_paths": sorted(files)},
                    )
                ]
            content = files[path]
            proof = _proof_for(workspace_manifest, path)
            for needle in spec.get("must_contain") or []:
                if needle not in content:
                    content_mismatches.append({
                        "path": path, "check": "must_contain",
                        "missing_substring": needle, "proof": proof,
                    })
            for needle in spec.get("must_not_contain") or []:
                if needle in content:
                    content_mismatches.append({
                        "path": path, "check": "must_not_contain",
                        "forbidden_substring": needle, "proof": proof,
                    })
            exact = spec.get("equals")
            if exact is not None and content != str(exact):
                content_mismatches.append({
                    "path": path, "check": "equals", "proof": proof,
                })
        if content_mismatches:
            return [_fold_content_mismatches(content_mismatches)]

        # --- CXT-5: destructive-elision scan over final deliverables (rule set v1) ---
        # A finished deliverable must not contain unrecoverable elision placeholder text
        # ("(elided)" / "[trimmed]" / "content omitted" / "truncated for brevity" with no
        # recover cue). Scoped to the asserted deliverables; waivable per scenario.
        if not workspace_assert.get("allow_elision"):
            for spec in workspace_assert.get("files") or []:
                path = spec.get("path")
                if path is None or path not in files:
                    continue
                hits = scan_for_destructive_elision(files[path])
                if hits:
                    return [
                        failing(
                            _ORACLE,
                            fc.DESTRUCTIVE_ELISION,
                            first_broken_link="finish -> deliverable_recoverable",
                            facts={"path": path, "offending_lines": hits[:5]},
                        )
                    ]

        # --- preview truth ---
        if preview_assert.get("required"):
            if not preview:
                return [
                    failing(
                        _ORACLE,
                        fc.FALSE_FINISH_PREVIEW_BROKEN,
                        first_broken_link="finish -> preview_health",
                        facts={"reason": "preview required but no preview evidence captured"},
                    )
                ]
            health = preview.get("health") or {}
            status = health.get("status")
            if status is not None and int(status) >= 400:
                return [
                    failing(
                        _ORACLE,
                        fc.FALSE_FINISH_PREVIEW_BROKEN,
                        first_broken_link="finish -> preview_health",
                        facts={"preview_health_status": status},
                    )
                ]
            preview_content = str(preview.get("content", ""))
            for needle in preview_assert.get("must_contain") or []:
                if needle not in preview_content:
                    return [
                        failing(
                            _ORACLE,
                            fc.PREVIEW_TRUTH_MISMATCH,
                            first_broken_link="finish -> preview_content",
                            facts={"missing_substring": needle},
                        )
                    ]

        return [passing(_ORACLE, facts={"terminal_status": term, "file_count": len(files)})]
