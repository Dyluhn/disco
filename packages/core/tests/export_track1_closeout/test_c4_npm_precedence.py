"""WO-C4 §8.8 closeout — npm precedence, launchers, and executable script proof.

The no-lockfile toolchain reject (``_unsupported_node_pm_declaration``, added on tip
``300d7ec6``) had two defects that an exhaustive adversarial re-verify raised against C4;
this frozen matrix pins the corrected contract at the ``disco.core.release`` detect leaf
(``detect_release`` called directly over a constructed file map — the level the defect
lives at; no route, no seam, no monkeypatch).

FINDING #1 (over-rejection / FALSE BLOCK). When an AUTHORITATIVE npm signal is present — a
committed root ``package-lock.json`` OR a corepack ``packageManager:"npm@…"`` field — a
co-present STRAY PASSIVE config marker (a leftover ``.yarnrc`` / ``pnpm-workspace.yaml`` /
``bunfig.toml`` from a yarn/pnpm/bun→npm migration) must NOT reject the release: npm is the
manager the neutral base image runs, so it WINS and the project stays a ``candidate``.
Pre-fix a single stray marker flipped an otherwise plain npm project to
``toolchain_unsupported`` — a false block of a legitimate self-host. (Closeout #2c: an
ACTIVE launcher in a script HEAD is the EXCEPTION — the npm signal does NOT override it,
because the app literally invokes that runtime; see finding #2c.)

FINDING #2c (silent-broken-bundle / MUST NOT over-relax finding #1). An authoritative npm
signal overrides only PASSIVE markers, NEVER an ACTIVE launcher in a
``start``/``build``/``prebuild``/``postbuild`` script head. A script that literally runs
``bunx``/``bun``/``pnpm``/``pnpx``/``yarn`` genuinely needs that runtime; silently lowering
it to ``npm start`` beside a stale ``package-lock.json`` would ship a broken bundle
(crit-8). The script-head-family loop therefore runs UNCONDITIONALLY — an active launcher
head is runtime-authoritative and rejects ``toolchain_unsupported`` even with a lockfile.

FINDING #2 (under-rejection). The script-head check was exact-match on {bun,pnpm,yarn} over
only ``start``/``build``, so an ``x``-suffixed launcher head (``"start":"bunx vite"``,
``"prebuild":"pnpx …"``) slipped through to a false npm candidate → runtime failure. The
fix matches launcher FAMILIES (``bun``/``bunx`` → bun, ``pnpm``/``pnpx`` → pnpm, ``yarn`` →
yarn) and also inspects ``prebuild``/``postbuild`` — only when there is NO authoritative
npm signal (finding #1 precedence still holds).

FINDING #2d (silent-broken-bundle via a HIDDEN launcher head — HIGH). The launcher-head
check parsed only the first whitespace token (``script.split()[0]``), so a launcher hidden
behind a leading ``NAME=VALUE`` env prefix, a shell chain (``&&`` / ``||`` / ``;`` / ``|`` /
``&``), or a ``corepack`` wrapper escaped the family match → false npm ``candidate`` →
emitted ``CMD ["npm","start"]`` shells to a missing ``bunx`` / ``pnpm`` / ``yarn`` at run
time. It also inspected only ``start`` / ``build``, not the install/start lifecycle hooks
the emitted bundle actually runs. The fix extracts the EFFECTIVE head of every shell
sub-command (stripping env-assignment / ``corepack`` prefixes) and inspects every key
``npm ci`` + ``npm run build`` + ``npm start`` provably run (``preinstall`` / ``install`` /
``postinstall``, ``prebuild`` / ``build`` / ``postbuild``, ``prestart`` / ``start`` /
``poststart``). A launcher HEAD in any inspected key → ``toolchain_unsupported``; a launcher
name that is only a non-head ARGUMENT stays a candidate. (#2d initially excluded ``prepare``;
#2e CORRECTS that — see below.)

FINDING #2e (prepare lifecycle hook + transparent-wrapper unwrap; documented opaque-wrapper
ceiling — the FINAL launcher-detection closeout). (A) ``npm ci`` DOES run ``preprepare`` /
``prepare`` / ``postprepare`` (verified on npm 10.9.7, the emitted ``node:22-bookworm-slim``
image's npm major), so a launcher in ``prepare`` (``prepare:"bunx build"``) crashes the
emitted ``RUN ["npm","ci"]`` exactly like ``postinstall`` — those three keys are now
inspected (the #2d exclusion rested on a false premise). (B) ``_effective_head`` now unwraps
the TRANSPARENT-PREFIX wrapper family (``corepack`` / ``cross-env`` / ``env`` / ``exec`` /
``dotenv``), which exec the rest of the line, so ``cross-env FOO=1 bunx vite``, ``env bunx
x``, ``exec bunx x``, and ``dotenv -- bunx x`` resolve to the real launcher head. The
opaque/quoted-wrapper class (``sh -c "…"``, ``concurrently "…"``, ``$(…)``, quoted heads,
flag-arg wrappers like ``nice`` / ``dotenv -e .env``) is an ACCEPTED static-parse ceiling,
documented at the detection site — not a shape a real production start script uses.

RED vs GREEN (BEFORE each fix):
  * RED (finding #1, tip 300d7ec6) — ``package-lock.json`` + ``.yarnrc`` and
    ``packageManager:"npm@10"`` + ``pnpm-workspace.yaml`` are WRONGLY rejected
    ``toolchain_unsupported`` (the stray MARKER wins over the authoritative npm signal).
    The fix makes them ``candidate``.
  * RED (finding #2, tip 300d7ec6) — ``"start":"bunx vite"`` and a ``pnpx`` head with NO
    npm signal are WRONGLY classified ``candidate`` (the ``x``-launcher head is not
    caught). The fix rejects them ``toolchain_unsupported``.
  * RED (closeout #2c, tip 48133f0f) — ``package-lock.json`` + ``"start":"bunx vite"`` (an
    ACTIVE launcher head beside an authoritative npm signal) was WRONGLY a ``candidate``:
    the initial finding-#1 fix over-broadly let the npm signal override script heads too.
    #2c makes the active launcher head runtime-authoritative — it rejects
    ``toolchain_unsupported`` even with a lockfile, while the PASSIVE marker cases above
    still stay ``candidate``.
  * RED (closeout #2d, tip a78f6b70) — a launcher hidden behind an env prefix
    (``NODE_ENV=production bunx vite``) or a shell chain (``cd app && bunx vite``), or living
    in an install/start lifecycle hook (``postinstall`` / ``preinstall`` / ``prestart`` /
    …), beside a committed ``package-lock.json``, was WRONGLY a ``candidate``. Robust
    extraction rejects it ``toolchain_unsupported``, while benign chains/hooks (every
    effective head node/npm/npx/…, or a launcher only as a non-head arg) stay ``candidate``.
  * RED (closeout #2e, tip b265c08a) — ``prepare:"pnpm build"`` + ``package-lock.json`` (npm
    ci runs prepare), and a launcher behind a transparent wrapper (``cross-env FOO=1 bunx
    vite``, ``dotenv -- bunx x``), were WRONGLY ``candidate`` (prepare uninspected; only
    ``corepack`` unwrapped). The fix rejects them; benign wrapper heads (``cross-env … node``,
    ``env node x``) and benign ``prepare`` (``husky install``, ``node …``) stay ``candidate``.
  * GREEN preservation — a plain pnpm/yarn/bun declaration with NO npm signal STILL fails
    closed; a plain npm project (incl. one with an authoritative-npm-overridden stray
    PASSIVE marker) STILL a candidate; a NON-npm ``packageManager`` field STILL rejects
    even beside a (stale) ``package-lock.json``.

Randomized (plan §4 crit 8): the ``package.json`` ``"name"`` is drawn from the seeded
``closeout_name`` factory. The detector classifies on scripts / ``packageManager`` /
lockfile / markers — NEVER the package name — so the verdict cannot be recognized from a
hard-coded name. No mock/patch and no monkeypatch: the file map is constructed and
``detect_release`` is called directly (anti-bypass §4.4).

ACCEPTANCE-v5 CONTROLLED RE-FREEZE (R7; supersedes the old broad-benign conclusion above,
without changing the preserved launcher/marker cases).  The old fifteen-case benign table
proved only that a head was not bun/pnpm/yarn; it did not prove that a referenced file or
package would exist after ``npm ci``.  Exactly two no-extra-artifact forms remain positive:
``NODE_ENV=production node server.js`` and ``exec node server.js``.  The other thirteen
forms are explicit RED contracts for one ``entrypoint_unresolved`` blocker on the
lifecycle field that would execute the unresolved script.  Four paired GREEN controls add
the referenced node file.  Those six positives prevent a blanket-rejection implementation
from satisfying the re-freeze.
"""

from __future__ import annotations

import json

import pytest
from disco.core.release.detect import DetectionResult, Provenance, detect_release
from disco.core.release.spec import ReleaseAssessment, ServiceRole

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


def _detect(files: dict[str, str]) -> DetectionResult:
    return detect_release(files, intent=None, provenance=Provenance())


def _blocker_codes(result: DetectionResult) -> set[str]:
    return {blocker.code for blocker in result.blockers}


def _assert_candidate(result: DetectionResult) -> None:
    assert getattr(result, "assessment", None) is ReleaseAssessment.candidate, (
        "an authoritative npm signal must keep the project a self-host candidate; "
        f"got assessment={getattr(result, 'assessment', None)!r}, "
        f"blockers={sorted(_blocker_codes(result))}."
    )
    assert _TOOLCHAIN_UNSUPPORTED not in _blocker_codes(result), (
        "a co-present stray non-npm marker/script head must not emit toolchain_unsupported "
        f"when npm is authoritatively declared; saw {sorted(_blocker_codes(result))}."
    )


def _assert_locked_npm_candidate(
    result: DetectionResult, *, expected_build_cmd: tuple[str, ...]
) -> None:
    """Assert the complete runnable contract; a broad "candidate" bit is insufficient.

    Every acceptance-v5 positive carries a committed npm lock and must resolve to exactly
    one ingress whose emitted lifecycle is ``npm ci`` -> optional ``npm run build`` ->
    ``npm start``.  The empty blocker/resource/env assertions make the controls teethful
    against implementations that retain a partial candidate beside diagnostics.
    """
    _assert_candidate(result)
    assert result.blockers == ()
    assert result.resources == ()
    assert result.env == ()
    assert len(result.services) == 1
    ingress = result.services[0]
    assert ingress.role is ServiceRole.ingress
    assert result.ingress == ingress
    assert ingress.package_manager == "npm"
    assert ingress.lockfile == "package-lock.json"
    assert ingress.install_cmd == ("npm", "ci")
    assert ingress.build_cmd == expected_build_cmd
    assert ingress.start_cmd == ("npm", "start")


def _assert_entrypoint_unresolved(result: DetectionResult, *, field: str) -> None:
    """Assert the exact fail-closed shape; an unrelated or blanket reject cannot pass."""
    assert result.assessment is ReleaseAssessment.needs_review
    assert result.services == ()
    assert result.resources == ()
    assert result.env == ()
    assert result.ingress is None
    assert len(result.blockers) == 1
    blocker = result.blockers[0]
    assert blocker.code == "entrypoint_unresolved"
    assert blocker.field == field
    assert _blocker_codes(result) == {"entrypoint_unresolved"}


def _assert_toolchain_unsupported(result: DetectionResult) -> None:
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
    ("bunfig.toml", '[install]\nregistry = "https://registry.example"\n'),
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
        "package.json": _package_json(str(closeout_name("svc")), {"start": "node server.js"}, None),
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


@pytest.mark.parametrize(
    ("script_key", "script"),
    [
        ("start", "bunx vite"),
        ("start", "pnpx dev"),
        ("start", "yarn start"),
        ("build", "pnpm build"),
        ("build", "bunx --bun vite build"),
        ("prebuild", "pnpx codegen"),
    ],
)
def test_active_launcher_head_rejects_even_with_authoritative_npm(
    script_key: str, script: str, closeout_name: object
) -> None:
    """RED on 48133f0f (closeout #2c). An ACTIVE launcher in a ``start``/``build``/
    ``prebuild``/``postbuild`` script head is RUNTIME-authoritative: the app literally
    invokes bun/pnpm/yarn, so a committed ``package-lock.json`` does NOT rescue it —
    silently lowering it to ``npm start`` would ship a broken bundle (crit-8). It must fail
    closed to ``toolchain_unsupported`` even with an authoritative npm signal present.
    Contrast a PASSIVE config marker beside the same npm signal, which stays a candidate
    (``test_committed_package_lock_beats_stray_marker``). Pre-#2c the finding-#1 guard
    skipped the script-head loop whenever npm was authoritative, so these were a false
    ``candidate``; #2c runs the script-head loop unconditionally."""
    assert callable(closeout_name)
    scripts = {"start": "node server.js"}
    scripts[script_key] = script  # may overwrite start with the launcher head under test
    files = {
        "package.json": _package_json(str(closeout_name("svc")), scripts, None),
        "package-lock.json": _PACKAGE_LOCK,
        "server.js": _SERVER_JS,
    }
    _assert_toolchain_unsupported(_detect(files))


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


# ---- FINDING #2d: robust head extraction — env-prefix / shell-chain / corepack + hooks ----

# Head-hiding shapes whose EFFECTIVE launcher head the naive ``split()[0]`` parse missed,
# each WITH a committed ``package-lock.json`` (so it also proves the #2c UNCONDITIONAL reject
# survives the hiding: an authoritative npm signal never rescues a live launcher).
_HIDDEN_LAUNCHER_CASES: tuple[tuple[str, str], ...] = (
    ("start", "NODE_ENV=production bunx vite"),  # leading env-assignment prefix
    ("start", "cd app && bunx vite"),  # `&&` chain, launcher in 2nd sub-command
    ("build", "vite build && bunx compress"),  # `&&` chain, benign head first
    ("start", "true ; pnpm dev"),  # `;` sequence
    ("start", "vite build | pnpx bundle"),  # `|` pipe
    ("start", "node warmup.js & bunx serve"),  # `&` background
    ("start", "corepack pnpm start"),  # corepack wrapper
    ("start", "NODE_ENV=prod corepack yarn dev"),  # env prefix + corepack wrapper
    ("preinstall", "pnpm run gen"),  # npm ci install-lifecycle hook
    ("install", "yarn build"),  # npm ci install-lifecycle hook
    ("postinstall", "bunx patch"),  # npm ci install-lifecycle hook
    ("prestart", "yarn warmup"),  # npm start pre-hook
    ("poststart", "pnpx notify"),  # npm start post-hook
    # #2e (A): `npm ci` runs the prepare lifecycle (npm 10.9.7 / node:22) -> a launcher in
    # prepare/preprepare/postprepare crashes the emitted `npm ci`.
    ("prepare", "pnpm build"),
    ("preprepare", "yarn gen"),
    ("postprepare", "pnpx notify"),
    # #2e (B): a launcher behind a transparent-prefix wrapper (`corepack` was already caught;
    # `cross-env`/`env`/`exec`/`dotenv` are the new family) still runs at bundle time.
    ("start", "cross-env FOO=1 bunx vite"),
    ("start", "cross-env NODE_ENV=prod pnpm dev"),
    ("start", "env bunx x"),
    ("start", "exec bunx x"),
    ("start", "dotenv -- bunx x"),
    ("start", "env cross-env bunx x"),  # stacked wrappers resolve
)


@pytest.mark.parametrize(("script_key", "script"), _HIDDEN_LAUNCHER_CASES)
def test_hidden_launcher_head_rejects_even_with_authoritative_npm(
    script_key: str, script: str, closeout_name: object
) -> None:
    """RED on a78f6b70 (closeout #2d). A non-npm launcher hidden behind an env prefix, a
    shell chain (``&&``/``||``/``;``/``|``/``&``), or a ``corepack`` wrapper — or living in an
    install/start lifecycle hook the emitted bundle actually runs (``npm ci`` ->
    pre/install/post; ``npm start`` -> pre/post) — was missed by the naive ``split()[0]``
    parse and shipped a FALSE npm ``candidate`` whose ``CMD ["npm","start"]`` shells to a
    missing ``bunx``/``pnpm``/``yarn`` at run time (silent-broken-bundle). Robust extraction
    must fail it closed to ``toolchain_unsupported`` even with a committed
    ``package-lock.json`` present (#2c: a live launcher is runtime-authoritative)."""
    assert callable(closeout_name)
    scripts = {"start": "node server.js"}
    scripts[script_key] = script  # may overwrite start with the head-hiding launcher
    files = {
        "package.json": _package_json(str(closeout_name("svc")), scripts, None),
        "package-lock.json": _PACKAGE_LOCK,
        "server.js": _SERVER_JS,
    }
    _assert_toolchain_unsupported(_detect(files))


# Acceptance-v5: these thirteen old "benign" shapes are not proven executable.  Each
# carries a committed npm lock so package-manager selection is settled; the ONLY question
# is whether the lifecycle script can be resolved in the clean image.  IDs are literal and
# mirrored byte-for-byte in ``controlled_refreeze_r7.reclassified_reds``.
_UNRESOLVED_NPM_SCRIPT_CASES: tuple[object, ...] = (
    pytest.param("build", "cd app && npm run build", "build_cmd", id="build-cd-self-recursion"),
    pytest.param(
        "build",
        "vite build && node post.js",
        "build_cmd",
        id="build-vite-chain-missing-post",
    ),
    pytest.param("start", "npx serve", "start_cmd", id="start-npx-serve-absent-dep"),
    pytest.param(
        "start",
        'echo "use bun" && node server.js',
        "start_cmd",
        id="start-echo-chain",
    ),
    pytest.param(
        "postinstall",
        "node scripts/patch.js",
        "install_cmd",
        id="install-postinstall-missing-node-file",
    ),
    pytest.param(
        "postinstall",
        "patch-package",
        "install_cmd",
        id="install-postinstall-patch-package-absent",
    ),
    pytest.param(
        "prestart",
        "node warmup.js",
        "start_cmd",
        id="start-prestart-missing-node-file",
    ),
    pytest.param("prepare", "husky install", "install_cmd", id="install-prepare-husky-absent"),
    pytest.param(
        "prepare",
        "node scripts/x.js",
        "install_cmd",
        id="install-prepare-missing-node-file",
    ),
    pytest.param(
        "prepare",
        "patch-package",
        "install_cmd",
        id="install-prepare-patch-package-absent",
    ),
    pytest.param(
        "start",
        "cross-env NODE_ENV=production node server.js",
        "start_cmd",
        id="start-cross-env-absent",
    ),
    pytest.param("start", "env node x", "start_cmd", id="start-env-missing-node-file"),
    pytest.param(
        "start",
        "dotenv -- npm run start",
        "start_cmd",
        id="start-dotenv-self-recursion",
    ),
)


@pytest.mark.parametrize(("script_key", "script", "expected_field"), _UNRESOLVED_NPM_SCRIPT_CASES)
def test_unresolved_npm_lifecycle_script_fails_closed(
    script_key: str, script: str, expected_field: str, closeout_name: object
) -> None:
    """RED on c0c4f728: unresolved lifecycle work must not become a runnable bundle."""
    assert callable(closeout_name)
    scripts = {"start": "node server.js"}
    scripts[script_key] = script
    files = {
        "package.json": _package_json(str(closeout_name("svc")), scripts, None),
        "package-lock.json": _PACKAGE_LOCK,
        "server.js": _SERVER_JS,
    }
    result = _detect(files)
    _assert_entrypoint_unresolved(result, field=expected_field)
    if script == 'echo "use bun" && node server.js':
        # ``bun`` is an echo argument, not a launcher.  This case must fail for unresolved
        # start-command proof and never regress to the unrelated toolchain classifier.
        assert _TOOLCHAIN_UNSUPPORTED not in _blocker_codes(result)


# The two retained positives need no package or file beyond the already-proven server.
_RETAINED_WRAPPER_CASES: tuple[object, ...] = (
    pytest.param("NODE_ENV=production node server.js", id="env-prefix-node"),
    pytest.param("exec node server.js", id="exec-node"),
)


@pytest.mark.parametrize("start_script", _RETAINED_WRAPPER_CASES)
def test_retained_benign_wrapper_stays_candidate(start_script: str, closeout_name: object) -> None:
    """GREEN: the two accepted wrappers resolve to the present, PORT-bound server."""
    assert callable(closeout_name)
    files = {
        "package.json": _package_json(str(closeout_name("svc")), {"start": start_script}, None),
        "package-lock.json": _PACKAGE_LOCK,
        "server.js": _SERVER_JS,
    }
    _assert_locked_npm_candidate(_detect(files), expected_build_cmd=())


# Paired controls for four file-reference reds.  Each uses the same lifecycle position and
# command family as its red sibling, but adds the exact referenced file; blanket rejection
# therefore fails these controls.  Empty JS programs are valid and deterministically exit 0.
_PRESENT_NODE_REFERENCE_CASES: tuple[object, ...] = (
    pytest.param(
        "build",
        "node build.js",
        "build.js",
        ("npm", "run", "build"),
        id="build-node-file",
    ),
    pytest.param(
        "postinstall",
        "node scripts/patch.js",
        "scripts/patch.js",
        (),
        id="postinstall-node-file",
    ),
    pytest.param("prestart", "node warmup.js", "warmup.js", (), id="prestart-node-file"),
    pytest.param("prepare", "node scripts/x.js", "scripts/x.js", (), id="prepare-node-file"),
)


@pytest.mark.parametrize(
    ("script_key", "script", "referenced_file", "expected_build_cmd"),
    _PRESENT_NODE_REFERENCE_CASES,
)
def test_present_node_script_reference_stays_candidate(
    script_key: str,
    script: str,
    referenced_file: str,
    expected_build_cmd: tuple[str, ...],
    closeout_name: object,
) -> None:
    """GREEN: an existing node lifecycle target remains a fully runnable candidate."""
    assert callable(closeout_name)
    scripts = {"start": "node server.js", script_key: script}
    files = {
        "package.json": _package_json(str(closeout_name("svc")), scripts, None),
        "package-lock.json": _PACKAGE_LOCK,
        "server.js": _SERVER_JS,
        referenced_file: "// present acceptance-v5 control\n",
    }
    _assert_locked_npm_candidate(_detect(files), expected_build_cmd=expected_build_cmd)


# ---- GREEN preservation: the genuine reject must NOT be weakened ----


@pytest.mark.parametrize(("marker", "content"), _STRAY_MARKERS)
def test_stray_marker_without_npm_signal_still_fails_closed(
    marker: str, content: str, closeout_name: object
) -> None:
    """GREEN preservation. With NO authoritative npm signal, a non-npm workspace/config
    marker STILL fails closed — finding #1's precedence must not open a blanket bypass."""
    assert callable(closeout_name)
    files = {
        "package.json": _package_json(str(closeout_name("svc")), {"start": "node server.js"}, None),
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
        "package.json": _package_json(str(closeout_name("svc")), {"start": "node server.js"}, None),
        "package-lock.json": _PACKAGE_LOCK,
        "server.js": _SERVER_JS,
    }
    _assert_candidate(_detect(files))
