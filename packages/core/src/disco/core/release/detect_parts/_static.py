"""Static-bundle detection: the Vite output-directory resolver, the source-ladder
static detector, and the typed-intent static ladder rung.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from disco.core.release.spec import (
    ReleaseAssessment,
    ReleaseIntent,
    ReleaseService,
    RuntimeStrategy,
    ServiceRole,
)

from ._blockers import (
    _command_grammar_blocker,
    _declared_manager_blocker,
    _resource_topology_blocker,
)
from ._constants import (
    _INGRESS_ID,
    _UNSUPPORTED_NODE_PM,
    _VITE_CONFIG_NAMES,
    _VITE_OUTDIR_KEY_RE,
    _VITE_OUTDIR_LITERAL_RE,
)
from ._env import (
    _intent_build_env,
    _intent_env_result,
    _secret_build_blocker,
    _secret_build_env_blocker,
)
from ._models import DetectionResult, _DetectBlocker
from ._node import (
    _lockfile_conflict,
    _node_install,
    _root_package_json,
    _script,
    _unsupported_node_pm_declaration,
    _unsupported_pm_blocker,
)
from ._results import _fail_closed
from ._text import _basename, _paths, _scan_text, _strip_comments


def _static_output_dir(files: Mapping[str, str | bytes], build_script: str) -> str | None:
    """Resolve a build-requiring static bundle's output directory, or `None` when it
    cannot be established (fail closed):

    * an unknown build tool (not a recognized `vite` build) -> `None`;
    * a Vite config with an EXPLICIT literal `outDir: '<x>'` -> `<x>`;
    * a Vite config with a DYNAMIC `outDir` (a non-literal value) -> `None`;
    * a provable default-Vite build (recognized `vite`, no `outDir` override) ->
      Vite's default `dist`."""
    if not re.search(r"\bvite\b", build_script):
        return None  # unknown bundler — output dir not statically knowable
    for path in files:
        if _basename(path) in _VITE_CONFIG_NAMES:
            # Strip comments first: a decoy `// outDir: 'dist'` must not be read as a
            # resolved output directory when the real config is dynamic/absent.
            text = _strip_comments(_scan_text(files[path]))
            literal = _VITE_OUTDIR_LITERAL_RE.search(text)
            if literal is not None:
                return literal.group(1)
            if _VITE_OUTDIR_KEY_RE.search(text):
                return None  # dynamic/computed outDir — fail closed
    return "dist"  # default-Vite: no outDir override


def _static_detect(
    files: Mapping[str, str | bytes],
) -> ReleaseService | _DetectBlocker | None:
    """Detect a static ingress from a root `index.html` (with no node `start`).

    A no-build site serves the workspace root; a build-requiring bundle must resolve
    a statically-known output directory, else it fails closed
    (`output_dir_unresolved`)."""
    if "index.html" not in _paths(files):
        return None
    pkg = _root_package_json(files)
    build_script = _script(pkg, "build")
    if build_script is not None:
        output_dir = _static_output_dir(files, build_script)
        if output_dir is None:
            return _DetectBlocker(
                code="output_dir_unresolved",
                message=(
                    "a static build was detected but its output directory cannot be "
                    "resolved statically (an unknown build tool, or a dynamically "
                    "computed Vite `outDir`); the emitted bundle would serve the wrong "
                    "tree. Declare the output directory via a typed intent."
                ),
                field="output_dir",
                evidence=(
                    "static output evidence: build output directory not statically resolvable",
                ),
            )
        # A build-requiring static bundle installs with a package manager too, so the
        # same disagreement / unsupported-toolchain / build-secret guards apply (§8.9 /
        # §8.8 / §8.4).
        conflict = _lockfile_conflict(files)
        if conflict is not None:
            return conflict
        toolchain = _unsupported_pm_blocker(files, _UNSUPPORTED_NODE_PM)
        if toolchain is not None:
            return toolchain
        # An authoritative non-lockfile PM declaration also rejects a build-requiring
        # static bundle (same no-lockfile bypass, same §8.8 reject-not-map contract).
        declared_toolchain = _unsupported_node_pm_declaration(files, pkg)
        if declared_toolchain is not None:
            return declared_toolchain
        build_secret = _secret_build_blocker(files)
        if build_secret is not None:
            return build_secret
        # A SOURCE-DISCOVERED secret build var (a secret-shaped `import.meta.env.VITE_*`)
        # fails closed like an `.npmrc` build secret — never folded into build.args (§8.4).
        source_secret = _secret_build_env_blocker(files)
        if source_secret is not None:
            return source_secret
        # A build-requiring static bundle MUST install its dependencies before the
        # build runs, or the emitted image builds against an empty node_modules.
        lockfile, manager, install = _node_install(files)
        return ReleaseService(
            id=_INGRESS_ID,
            role=ServiceRole.ingress,
            runtime=RuntimeStrategy.static,
            package_manager=manager,
            lockfile=lockfile,
            install_cmd=install,
            build_cmd=("npm", "run", "build"),
            output_dir=output_dir,
            port_env="PORT",
        )
    return ReleaseService(
        id=_INGRESS_ID,
        role=ServiceRole.ingress,
        runtime=RuntimeStrategy.static,
        output_dir=".",
        port_env="PORT",
    )


def _static_from_intent(intent: ReleaseIntent, files: Mapping[str, str | bytes]) -> DetectionResult:
    """A typed intent describing a STATIC site — a prebuilt tree served as-is, or a
    build-then-serve bundle whose built assets land in `output_dir` (R2 / G03 / G06). A
    static site is SERVED, not started, so it legitimately has no `start_cmd`. Fails
    closed on an unprovisioned toolchain or a build-time secret; otherwise a single
    static ingress served from `output_dir` (defaulting to the workspace root)."""
    # Deferred: `_intent` imports `_static_from_intent` from this module at module
    # scope, so this module must not import `_intent` back at module scope (a genuine
    # two-way dependency the original single-file layout never had to name). Resolved
    # lazily — by call time both modules are fully loaded.
    from ._intent import _intent_evidence, _intent_install

    manager_blocker = _declared_manager_blocker(intent)
    if manager_blocker is not None:
        return _fail_closed(manager_blocker)
    # A raw sidecar reaches emission without the tool's grammar gate, so re-apply it to
    # the build_cmd (start is empty here) + the migrate/positional credential rails.
    grammar = _command_grammar_blocker(intent)
    if grammar is not None:
        return _fail_closed(grammar)
    # R3 (G07): a static intent may still declare a stateful resource; a filesystem-ROOT
    # persistent path is unbackable (its mount dir is `/`), so fail closed here too.
    topology = _resource_topology_blocker(intent.resources)
    if topology is not None:
        return _fail_closed(topology)
    # A build ALWAYS installs first, so derive an install even for a no-dependency
    # package.json (force_node); a prebuilt static serve (no build_cmd) installs nothing.
    install_outcome = _intent_install(
        intent, files, RuntimeStrategy.static, force_node=bool(intent.build_cmd)
    )
    if isinstance(install_outcome, _DetectBlocker):
        return _fail_closed(install_outcome)
    install_cmd, package_manager, lockfile = install_outcome
    env = _intent_env_result(intent)
    if isinstance(env, _DetectBlocker):
        return _fail_closed(env)
    env = _intent_build_env(env, intent, files)
    if isinstance(env, _DetectBlocker):
        return _fail_closed(env)
    service = ReleaseService(
        id=_INGRESS_ID,
        role=ServiceRole.ingress,
        runtime=RuntimeStrategy.static,
        package_manager=package_manager,
        lockfile=lockfile,
        install_cmd=install_cmd,
        build_cmd=intent.build_cmd,
        output_dir=intent.output_dir if intent.output_dir is not None else ".",
        port_env=intent.port_env,
        health_path=intent.health_path,
    )
    return DetectionResult(
        assessment=ReleaseAssessment.candidate,
        services=(service,),
        resources=intent.resources,
        env=env,
        evidence=(_intent_evidence(intent),),
        reasons=(
            "static release shape declared by a typed release intent "
            "(build command, output directory, and env names supplied).",
        ),
    )
