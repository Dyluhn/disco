"""WO-C4 §8.8 closeout — authoritative-npm precedence + launcher-family script heads.

The no-lockfile toolchain reject (``_unsupported_node_pm_declaration``, added on tip
``300d7ec6``) had two defects that an exhaustive adversarial re-verify raised against C4;
this frozen matrix pins the corrected contract at the ``disco.core.release`` detect leaf
(``detect_release`` called directly over a constructed file map — the level the defect
lives at; no route, no seam, no monkeypatch).

FINDING #1 (over-rejection / FALSE BLOCK). When an AUTHORITATIVE npm signal is present — a
committed root ``package-lock.json`` OR a corepack ``packageManager:"npm@…"`` field — a
co-present STRAY non-npm marker (a leftover ``.yarnrc`` / ``pnpm-workspace.yaml`` /
``bunfig.toml`` from a yarn/pnpm/bun→npm migration) or a non-npm script head must NOT
reject the release: npm is the manager the neutral base image runs, so it WINS and the
project stays a ``candidate``. Pre-fix a single stray marker/head flipped an otherwise
plain npm project to ``toolchain_unsupported`` — a false block of a legitimate self-host.

FINDING #2 (under-rejection). The script-head check was exact-match on {bun,pnpm,yarn} over
only ``start``/``build``, so an ``x``-suffixed launcher head (``"start":"bunx vite"``,
``"prebuild":"pnpx …"``) slipped through to a false npm candidate → runtime failure. The
fix matches launcher FAMILIES (``bun``/``bunx`` → bun, ``pnpm``/``pnpx`` → pnpm, ``yarn`` →
yarn) and also inspects ``prebuild``/``postbuild`` — only when there is NO authoritative
npm signal (finding #1 precedence still holds).

RED vs GREEN on tip ``300d7ec6`` (BEFORE the fix):
  * RED (finding #1) — ``package-lock.json`` + ``.yarnrc`` and ``packageManager:"npm@10"``
    + ``pnpm-workspace.yaml`` are WRONGLY rejected ``toolchain_unsupported`` (the stray
    marker wins over the authoritative npm signal). The fix makes them ``candidate``.
  * RED (finding #2) — ``"start":"bunx vite"`` and a ``pnpx`` head with NO npm signal are
    WRONGLY classified ``candidate`` (the ``x``-launcher head is not caught). The fix
    rejects them ``toolchain_unsupported``.
  * GREEN preservation — a plain pnpm/yarn/bun declaration with NO npm signal STILL fails
    closed; a plain npm project (with or without an authoritative-npm-overridden stray
    marker) STILL a candidate; a NON-npm ``packageManager`` field STILL rejects even
    beside a (stale) ``package-lock.json``.

Randomized (plan §4 crit 8): the ``package.json`` ``"name"`` is drawn from the seeded
``closeout_name`` factory. The detector classifies on scripts / ``packageManager`` /
lockfile / markers — NEVER the package name — so the verdict cannot be recognized from a
hard-coded name. No mock/patch and no monkeypatch: the file map is constructed and
``detect_release`` is called directly (anti-bypass §4.4).
"""

from __future__ import annotations

import json

import pytest
from disco.core.release.detect import Provenance, detect_release
from disco.core.release.spec import ReleaseAssessment

pytestmark = pytest.mark.export_track1_closeout

_TOOLCHAIN_UNSUPPORTED = "toolchain_unsupported"

# A node server that binds the $PORT contract and reads no undeclared env — enough of a
# node signature for the detector to reach the package-manager-declaration check (else it
# would fail earlier on port_contract_unresolved and never exercise the toolchain gate).
_SERVER_JS = "require('http').createServer((_q,r)=>r.end('ok')).listen(process.env.PORT);\n"

# A committed npm lockfile and a plain package.json scripts block — the authoritative-npm
# lockfile signal and the neutral start script the reject must never key on.
_PACKAGE_LOCK = json.dumps({"lockfileVersion": 3, "name": "svc"})


def _package_json(name: str, scripts: dict[str, str], package_manager: str | None) -> str:
    """A root ``package.json`` with the given scripts and optional corepack field. The
    ``name`` (a seeded closeout name) is inert to detection — it proves randomization
    without letting the verdict key on a fixed string."""
    payload: dict[str, object] = {"name": name, "scripts": scripts}
    if package_manager is not None:
        payload["packageManager"] = package_manager
    return json.dumps(payload)


def _detect(files: dict[str, str]) -> object:
    return detect_release(files, intent=None, provenance=Provenance())


def _blocker_codes(result: object) -> set[str]:
    assert hasattr(result, "blockers")
    return {blocker.code for blocker in result.blockers}


def _assert_candidate(result: object) -> None:
    assert getattr(result, "assessment", None) is ReleaseAssessment.candidate, (
        "an authoritative npm signal must keep the project a self-host candidate; "
        f"got assessment={getattr(result, 'assessment', None)!r}, "
        f"blockers={sorted(_blocker_codes(result))}."
    )
    assert _TOOLCHAIN_UNSUPPORTED not in _blocker_codes(result), (
        "a co-present stray non-npm marker/script head must not emit toolchain_unsupported "
        f"when npm is authoritatively declared; saw {sorted(_blocker_codes(result))}."
    )


def _assert_toolchain_unsupported(result: object) -> None:
    assert getattr(result, "assessment", None) is ReleaseAssessment.needs_review, (
        "a non-npm toolchain declaration with NO authoritative npm signal must fail "
        f"closed to needs_review; got assessment={getattr(result, 'assessment', None)!r}."
    )
    assert _TOOLCHAIN_UNSUPPORTED in _blocker_codes(result), (
        f"expected the exact typed blocker {_TOOLCHAIN_UNSUPPORTED!r}; "
        f"saw {sorted(_blocker_codes(result))}."
    )


# ---- FINDING #1: authoritative-npm precedence over a co-present stray marker ----

# Every stray non-npm workspace/config marker the detector recognizes — each is a plausible
# migration leftover that pre-fix wrongly rejected a committed-npm project.
_STRAY_MARKERS: tuple[tuple[str, str], ...] = (
    (".yarnrc", "registry=https://registry.example\n"),
    (".yarnrc.yml", "nodeLinker: node-modules\n"),
    ("bunfig.toml", "[install]\nregistry = \"https://registry.example\"\n"),
    ("pnpm-workspace.yaml", "packages:\n  - 'apps/*'\n"),
    ("pnpm-workspace.yml", "packages:\n  - 'apps/*'\n"),
)


@pytest.mark.parametrize(("marker", "content"), _STRAY_MARKERS)
def test_committed_package_lock_beats_stray_marker(
    marker: str, content: str, closeout_name: object
) -> None:
    """RED on 300d7ec6 (finding #1). A committed ``package-lock.json`` (authoritative npm)
    plus a leftover non-npm config marker is a realistic migration remnant; npm wins and it
    stays a candidate. Pre-fix the marker wrongly emitted ``toolchain_unsupported``."""
    assert callable(closeout_name)
    files = {
        "package.json": _package_json(
            str(closeout_name("svc")), {"start": "node server.js"}, None
        ),
        "package-lock.json": _PACKAGE_LOCK,
        marker: content,
        "server.js": _SERVER_JS,
    }
    _assert_candidate(_detect(files))


@pytest.mark.parametrize(("marker", "content"), _STRAY_MARKERS)
def test_npm_package_manager_field_beats_stray_marker(
    marker: str, content: str, closeout_name: object
) -> None:
    """RED on 300d7ec6 (finding #1). An explicit ``packageManager:"npm@10"`` (authoritative
    npm) plus a leftover non-npm config marker stays a candidate; npm wins. Pre-fix the
    marker wrongly emitted ``toolchain_unsupported``. No committed lockfile here, so the
    ``packageManager`` field is the sole authoritative npm signal under test."""
    assert callable(closeout_name)
    files = {
        "package.json": _package_json(
            str(closeout_name("svc")), {"start": "node server.js"}, "npm@10.5.0"
        ),
        marker: content,
        "server.js": _SERVER_JS,
    }
    _assert_candidate(_detect(files))


@pytest.mark.parametrize("head", ["bunx vite", "pnpx dev", "yarn start"])
def test_authoritative_npm_beats_a_stray_script_head(head: str, closeout_name: object) -> None:
    """RED on 300d7ec6 (finding #1). A committed ``package-lock.json`` also overrides a
    non-npm ``start`` script head (a leftover launcher line) — the marker/script-head
    rejection is skipped whenever npm is authoritatively declared. Pins that finding #1
    covers script heads, not only config markers."""
    assert callable(closeout_name)
    files = {
        "package.json": _package_json(str(closeout_name("svc")), {"start": head}, None),
        "package-lock.json": _PACKAGE_LOCK,
        "server.js": _SERVER_JS,
    }
    _assert_candidate(_detect(files))


# ---- FINDING #2: launcher-family script heads + prebuild/postbuild, no npm signal ----


@pytest.mark.parametrize(
    ("script_key", "script"),
    [
        ("start", "bunx vite"),
        ("start", "pnpx dev"),
        ("build", "bunx --bun vite build"),
        ("build", "pnpx tsc"),
        ("prebuild", "pnpx codegen"),
        ("postbuild", "bunx compress"),
        ("start", "yarn start"),
    ],
)
def test_launcher_family_head_without_npm_signal_fails_closed(
    script_key: str, script: str, closeout_name: object
) -> None:
    """RED on 300d7ec6 (finding #2). A bun/pnpm/yarn launcher family head — INCLUDING the
    ``x``-suffixed runners ``bunx``/``pnpx`` the pre-fix exact-match missed — in
    ``start``/``build``/``prebuild``/``postbuild``, with NO authoritative npm signal, must
    fail closed to ``toolchain_unsupported`` rather than mapping to a false npm candidate.
    A ``start`` script is always present so the node signature is detected."""
    assert callable(closeout_name)
    scripts = {"start": "node server.js"}
    scripts[script_key] = script  # may overwrite start with the launcher head under test
    files = {
        "package.json": _package_json(str(closeout_name("svc")), scripts, None),
        "server.js": _SERVER_JS,
    }
    _assert_toolchain_unsupported(_detect(files))


# ---- GREEN preservation: the genuine reject must NOT be weakened ----


@pytest.mark.parametrize(("marker", "content"), _STRAY_MARKERS)
def test_stray_marker_without_npm_signal_still_fails_closed(
    marker: str, content: str, closeout_name: object
) -> None:
    """GREEN preservation. With NO authoritative npm signal, a non-npm workspace/config
    marker STILL fails closed — finding #1's precedence must not open a blanket bypass."""
    assert callable(closeout_name)
    files = {
        "package.json": _package_json(
            str(closeout_name("svc")), {"start": "node server.js"}, None
        ),
        marker: content,
        "server.js": _SERVER_JS,
    }
    _assert_toolchain_unsupported(_detect(files))


def test_non_npm_package_manager_field_rejects_even_with_stale_lockfile(
    closeout_name: object,
) -> None:
    """GREEN preservation. A corepack ``packageManager:"pnpm@8"`` is the MOST authoritative
    declaration; mapping it onto npm is the §8.8 failure. A co-present (stale)
    ``package-lock.json`` does NOT rescue it — it still fails closed. Guards that the
    authoritative-npm precedence never masks an explicit non-npm corepack declaration."""
    assert callable(closeout_name)
    files = {
        "package.json": _package_json(
            str(closeout_name("svc")), {"start": "node server.js"}, "pnpm@8.15.0"
        ),
        "package-lock.json": _PACKAGE_LOCK,
        "server.js": _SERVER_JS,
    }
    _assert_toolchain_unsupported(_detect(files))


def test_plain_npm_project_stays_a_candidate(closeout_name: object) -> None:
    """GREEN preservation. A plain npm node service (a committed ``package-lock.json``, no
    stray marker, no non-npm head) stays a self-host candidate with no toolchain reject."""
    assert callable(closeout_name)
    files = {
        "package.json": _package_json(
            str(closeout_name("svc")), {"start": "node server.js"}, None
        ),
        "package-lock.json": _PACKAGE_LOCK,
        "server.js": _SERVER_JS,
    }
    _assert_candidate(_detect(files))
