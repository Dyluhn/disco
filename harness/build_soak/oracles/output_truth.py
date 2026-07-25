"""OutputTruthOracle (guidelines §11.5, §16).

If the run says it FINISHED, the claimed output must actually exist (§7 truth
hierarchy: a model/agent saying "the build worked" never overrides workspace /
preview truth).

Checks (only when the run reached a finished/verified terminal state AND the
scenario asserts output):
  * required workspace files exist             -> FALSE_FINISH_NO_OUTPUT
  * required file contents present (must_contain) -> ARTIFACT_TRUTH_MISMATCH
  * preview health passes (when required)      -> FALSE_FINISH_PREVIEW_BROKEN
  * required UI text proven by a current governed verifier receipt
    (legacy non-governed runs fall back to captured preview source)
                                                -> PREVIEW_TRUTH_MISMATCH

Evidence shapes (captured by the runner; supplied as plain dicts):
  workspace_manifest: {"<relpath>": "<file content>", ...}
                  or  {"<relpath>": {"content": "..."}, ...}
                  or  {"files": {"<relpath>": "...", ...}}
  preview: {"health": {"status": 200|404|...}, "content": "<served source>"}
"""

from __future__ import annotations

import re
from typing import Any

from disco.core.observations import scan_for_destructive_elision

from .. import failure_codes as fc
from ..events import terminal_status
from .schema import OracleResult, failing, passing, skipping
from .workspace_contract import (
    is_platform_managed_path,
    join_workspace_relative,
    normalized_workspace_relative_path,
    resolve_open_assertion_root,
)

_ORACLE = "OutputTruthOracle"
_WHITESPACE_RUN_RE = re.compile(r"\s+")


def _content_normalized(text: object) -> str:
    """Collapse whitespace runs and case for a CONTENT comparison.

    Rendered visible text is extracted from the accessibility tree, which emits a
    newline at element boundaries. So `<h1>Node Seed <span>440028</span></h1>` —
    a page that visibly reads exactly "Node Seed 440028" — extracts as
    "Node Seed\n440028" and failed a `must_contain` of the heading. Whether the
    check passed depended on whether the model wrapped the seed in a block-level
    element, which is a STYLING choice: the check was testing layout while
    claiming to test content (counted seed 440028, p4_ff_node_basic).

    This does not weaken the check. Needle text separated by any OTHER content
    still does not match — only runs of whitespace collapse — so a missing
    string, a typo, or the wrong seed still fails. It is the same content-neutral
    relaxation already applied to capitalization below, for the same reason.
    """

    return _WHITESPACE_RUN_RE.sub(" ", str(text)).strip().casefold()


_FINISHED_STATES = frozenset({"FINISHED", "VERIFIED"})

# A content mismatch on one of these proof levels is non-authoritative ONLY while the adapter
# also reports that the captured bytes failed to reach the readiness gate's stability threshold.
# Once `content_stable is True`, content truth is authoritative even when sha identity remains
# unproven. Any OTHER proof level (raw_sha / rendered_readback / absent) OR an UNANNOTATED entry
# (legacy / proxy capture with no `proof` key — treated as authoritative so we never silently
# downgrade) stays a hard ARTIFACT_TRUTH_MISMATCH.
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


def _content_stable_for(manifest: dict[str, Any] | None, path: str) -> bool:
    """Whether the adapter observed this entry stable for the readiness threshold.

    Missing field / non-dict legacy entries are treated as not stable; legacy behavior is
    still preserved by the proof fold because a missing proof remains authoritative.
    """
    files = _nested_files(manifest)
    entry = files.get(path)
    if entry is None:
        entry = files.get(path.lstrip("/"))
    return isinstance(entry, dict) and entry.get("content_stable") is True


def _authoritative_mismatch(mismatch: dict[str, Any]) -> bool:
    return (
        mismatch.get("proof") not in _NON_AUTHORITATIVE_PROOF
        or mismatch.get("content_stable") is True
    )


def _fold_content_mismatches(mismatches: list[dict[str, Any]]):
    """DETERMINISTIC PRECEDENCE fold over ALL workspace-content mismatches in the run
    (Part A). A mismatch is AUTHORITATIVE iff its proof is authoritative OR its captured
    bytes reached the adapter's content-stability threshold. If ANY mismatch is authoritative
    → a hard ``ARTIFACT_TRUTH_MISMATCH`` (the content regression WINS; never masked). ONLY if
    EVERY mismatch is non-authoritative → INVALID_RUN ``WORKSPACE_SNAPSHOT_UNVERIFIED`` (the
    bytes were still churning at the capture deadline, so the capture is too uncertain to call
    a product failure). The full per-file evidence (proof + content_stable + path + check
    class) rides in `facts["mismatches"]` either way."""
    proven = [m for m in mismatches if _authoritative_mismatch(m)]
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


def _verified_visible_text(
    verified_claim_results: list[dict[str, Any]],
    verified_observed_facts: list[dict[str, Any]],
) -> list[str]:
    """Return exact text values proven or observed by the adjudicated receipt.

    This helper is intentionally target-neutral. A web DOM/accessibility verifier,
    a future native accessibility verifier, or another target adapter may prove a
    ``visible_text`` claim. Output truth does not infer modality or trust a model's
    prose; the governed oracle has already validated the issuer, receipt, target
    execution, revision/generation, evidence references, and freshness.
    """

    claims = [
        expected
        for claim in verified_claim_results
        if isinstance(claim, dict)
        and claim.get("kind") == "visible_text"
        and claim.get("status") == "pass"
        and isinstance((expected := claim.get("expected")), str)
        and bool(expected)
    ]
    facts = [
        value
        for fact in verified_observed_facts
        if isinstance(fact, dict)
        and fact.get("kind") == "visible_text"
        and isinstance((value := fact.get("value")), str)
        and bool(value)
    ]
    return [*claims, *facts]


class OutputTruthOracle:
    def check(
        self,
        events: list[dict[str, Any]],
        *,
        scenario: dict[str, Any] | None = None,
        workspace_manifest: dict[str, Any] | None = None,
        preview: dict[str, Any] | None = None,
        verified_claim_results: list[dict[str, Any]] | None = None,
        verified_observed_facts: list[dict[str, Any]] | None = None,
        verified_artifact_paths: list[str] | None = None,
    ) -> list[OracleResult]:
        assertions = (scenario or {}).get("assertions") or {}
        workspace_assert = assertions.get("workspace") or {}
        preview_assert = assertions.get("preview") or {}
        exact_paths = workspace_assert.get("exact_paths")
        wants_output = (
            bool(workspace_assert.get("files"))
            or exact_paths is not None
            or bool(preview_assert.get("required"))
        )

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

        # --- exact product-file-set truth -----------------------------------
        # This assertion is deliberately opt-in. Most valid builds have an open
        # file set; when a user/target explicitly requires an exact shape, compare
        # it against the adapter's complete authoritative snapshot. Host-owned
        # runtime/context evidence is outside the product contract, but arbitrary
        # hidden files are not.
        if isinstance(exact_paths, list):
            expected = sorted(str(path) for path in exact_paths)
            actual = sorted(path for path in files if not is_platform_managed_path(path))
            missing_paths = sorted(set(expected) - set(actual))
            unexpected_paths = sorted(set(actual) - set(expected))
            if missing_paths or unexpected_paths:
                facts = {
                    "expected_paths": expected,
                    "missing_paths": missing_paths,
                    "unexpected_paths": unexpected_paths,
                }
                if missing_paths:
                    return [
                        failing(
                            _ORACLE,
                            fc.FALSE_FINISH_NO_OUTPUT,
                            first_broken_link="finish -> exact_workspace_shape",
                            facts=facts,
                        )
                    ]
                return [
                    failing(
                        _ORACLE,
                        fc.ARTIFACT_TRUTH_MISMATCH,
                        first_broken_link="finish -> exact_workspace_shape",
                        facts=facts,
                    )
                ]

        # --- workspace file existence + content truth ---
        # Existence is judged FIRST and hard (a missing required deliverable is FALSE_FINISH,
        # never softened). Content checks (must_contain / must_not_contain / exact) across ALL
        # asserted files are COLLECTED with each file's capture proof + content-stability
        # level, then folded by deterministic precedence (Part A): one authoritative mismatch
        # makes the whole run a hard ARTIFACT_TRUTH_MISMATCH; all-non-authoritative →
        # INVALID_RUN snapshot-unverified.
        file_specs = workspace_assert.get("files") or []
        declared_paths: list[str] = []
        for spec in file_specs:
            raw_path = spec.get("path")
            if raw_path is None:
                continue
            path = normalized_workspace_relative_path(raw_path)
            if path is None:
                return [
                    failing(
                        _ORACLE,
                        fc.WORKSPACE_SNAPSHOT_UNVERIFIED,
                        first_broken_link="scenario_contract -> workspace_file_path",
                        facts={
                            "reason": (
                                "declared output path is not a normalized "
                                "POSIX workspace-relative path"
                            ),
                            "declared_path": raw_path,
                        },
                    )
                ]
            declared_paths.append(path)

        assertion_root, root_error = resolve_open_assertion_root(
            present_paths=files,
            declared_paths=declared_paths,
            verified_artifact_paths=verified_artifact_paths,
        )
        if assertion_root is None and root_error and root_error.get("reason"):
            return [
                failing(
                    _ORACLE,
                    fc.WORKSPACE_SNAPSHOT_UNVERIFIED,
                    first_broken_link="governed_artifact -> output_contract_root",
                    facts=root_error,
                )
            ]

        if assertion_root is None:
            # Root resolution failed, so no declared path can be resolved. Falling
            # into the per-spec loop below would blame whichever spec happens to be
            # FIRST — and that file may be present, served and governed-verified.
            # Counted restart seed 450001 died exactly this way: `required_path:
            # server.js` while `present_paths` and `verified_artifact_paths` both
            # listed server.js, because the genuinely absent package.json was
            # spec[1] and the loop never reached it. Name what is actually missing,
            # and never a file the run demonstrably produced.
            absent = [
                path
                for path in declared_paths
                if not any(
                    join_workspace_relative(root, path) in files
                    for root in (*(root_error or {}).get("verified_candidate_roots", ()), ".")
                )
            ]
            facts: dict[str, Any] = {
                "reason": "no candidate root satisfies every declared output path",
                "missing_declared_paths": absent,
                "present_declared_paths": [p for p in declared_paths if p not in absent],
                "declared_paths": list(declared_paths),
                "present_paths": sorted(files),
            }
            if root_error:
                facts.update({k: v for k, v in root_error.items() if k not in facts})
            if absent:
                facts["required_path"] = absent[0]
            return [
                failing(
                    _ORACLE,
                    fc.FALSE_FINISH_NO_OUTPUT if absent else fc.WORKSPACE_SNAPSHOT_UNVERIFIED,
                    first_broken_link=(
                        "finish -> required_file_exists"
                        if absent
                        else "governed_artifact -> output_contract_root"
                    ),
                    facts=facts,
                )
            ]

        content_mismatches: list[dict[str, Any]] = []
        for spec in file_specs:
            raw_path = spec.get("path")
            if raw_path is None:
                continue
            path = normalized_workspace_relative_path(raw_path)
            if path is None:
                continue  # rejected above
            resolved_path = (
                join_workspace_relative(assertion_root, path)
                if assertion_root is not None
                else None
            )
            if resolved_path is None or resolved_path not in files:
                facts: dict[str, Any] = {
                    "required_path": path,
                    "present_paths": sorted(files),
                }
                if root_error:
                    facts.update(root_error)
                return [
                    failing(
                        _ORACLE,
                        fc.FALSE_FINISH_NO_OUTPUT,
                        first_broken_link="finish -> required_file_exists",
                        facts=facts,
                    )
                ]
            content = files[resolved_path]
            proof = _proof_for(workspace_manifest, resolved_path)
            content_stable = _content_stable_for(workspace_manifest, resolved_path)
            # Case-INSENSITIVE: these assert natural-language content ("bakery")
            # against model-authored copy ("Hearth & Crumb Bakery"). Capitalization
            # is content-neutral; a case-sensitive check FAILed a correct page on
            # the 2026-07-09 overnight soak (ARTIFACT_TRUTH_MISMATCH on 'bakery').
            for needle in spec.get("must_contain") or []:
                if _content_normalized(needle) not in _content_normalized(content):
                    content_mismatches.append(
                        {
                            "path": path,
                            "resolved_path": resolved_path,
                            "check": "must_contain",
                            "missing_substring": needle,
                            "proof": proof,
                            "content_stable": content_stable,
                        }
                    )
            for needle in spec.get("must_not_contain") or []:
                # Normalizing here can only make a FORBIDDEN string easier to find,
                # so this stays fail-closed.
                if _content_normalized(needle) in _content_normalized(content):
                    content_mismatches.append(
                        {
                            "path": path,
                            "resolved_path": resolved_path,
                            "check": "must_not_contain",
                            "forbidden_substring": needle,
                            "proof": proof,
                            "content_stable": content_stable,
                        }
                    )
            exact = spec.get("equals")
            if exact is not None and content != str(exact):
                content_mismatches.append(
                    {
                        "path": path,
                        "resolved_path": resolved_path,
                        "check": "equals",
                        "proof": proof,
                        "content_stable": content_stable,
                    }
                )
        if content_mismatches:
            return [_fold_content_mismatches(content_mismatches)]

        # --- CXT-5: destructive-elision scan over final deliverables (rule set v1) ---
        # A finished deliverable must not contain unrecoverable elision placeholder text
        # ("(elided)" / "[trimmed]" / "content omitted" / "truncated for brevity" with no
        # recover cue). Scoped to the asserted deliverables; waivable per scenario.
        if not workspace_assert.get("allow_elision"):
            for spec in file_specs:
                raw_path = spec.get("path")
                if raw_path is None:
                    continue
                path = normalized_workspace_relative_path(raw_path)
                if path is None or assertion_root is None:
                    continue
                resolved_path = join_workspace_relative(assertion_root, path)
                if resolved_path not in files:
                    continue
                hits = scan_for_destructive_elision(files[resolved_path])
                if hits:
                    return [
                        failing(
                            _ORACLE,
                            fc.DESTRUCTIVE_ELISION,
                            first_broken_link="finish -> deliverable_recoverable",
                            facts={
                                "path": path,
                                "resolved_path": resolved_path,
                                "offending_lines": hits[:5],
                            },
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
            preview_needles = preview_assert.get("must_contain") or []
            if verified_claim_results is not None:
                proven_text = _verified_visible_text(
                    verified_claim_results,
                    verified_observed_facts or [],
                )
                missing = [
                    needle
                    for needle in preview_needles
                    if not any(
                        _content_normalized(needle) in _content_normalized(value)
                        for value in proven_text
                    )
                ]
                evidence_basis = "current_governed_visible_text_claims"
            else:
                # Backward compatibility for historical/non-governed scenarios.
                # Governed Build runs never take this branch: once a typed claim
                # contract exists, raw HTML/source cannot substitute for rendered
                # or target-specific UI truth.
                preview_content = str(preview.get("content", ""))
                missing = [
                    needle
                    for needle in preview_needles
                    if _content_normalized(needle) not in _content_normalized(preview_content)
                ]
                proven_text = []
                evidence_basis = "legacy_captured_preview_source"
            if missing:
                needle = missing[0]
                return [
                    failing(
                        _ORACLE,
                        fc.PREVIEW_TRUTH_MISMATCH,
                        first_broken_link=(
                            "finish -> preview_visible_text"
                            if verified_claim_results is not None
                            else "finish -> preview_content"
                        ),
                        facts={
                            "missing_substring": needle,
                            "evidence_basis": evidence_basis,
                            "verified_visible_text": proven_text,
                        },
                    )
                ]

        return [
            passing(
                _ORACLE,
                facts={
                    "terminal_status": term,
                    "file_count": len(files),
                    "assertion_root": assertion_root,
                },
            )
        ]
