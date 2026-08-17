"""Change-locality, command-inventory and rollback proofs for PKG-18.

These are the package-wide assertions that neither shape file owns on its own:

* **No command runs.**  Proven structurally — the whole PKG-18 surface imports
  and calls no process-spawning API, so "no SDK, signing, simulator or store
  command executed" follows from the absence of *any* execution path rather
  than from a promise about which commands were chosen.
* **No central target switch.**  Core's Build Platform and loop modules name no
  platform and no vendor toolchain, asserted by AST/text over the real source.
* **The proof surface is test-owned.**  Nothing under `current/packages/*/src` mentions
  it, so deleting these files is a complete rollback.
* **One lifecycle.**  The registry refuses a host-protected namespace, a
  duplicate identity, and non-exact data — there is no second registration path
  to smuggle a shape through.

**Neither Expo/React Native nor Tauri/Electron is supported.**  Nothing in this
package changes that, and no test here should be read as evidence that it did.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from build_platform_registration_shapes import (
    DESKTOP_OPERATIONS,
    DESKTOP_PROFILE_ID,
    MOBILE_OPERATIONS,
    MOBILE_PROFILE_ID,
    PROOF_NAMESPACE,
    build_registration_registry,
    register_mobile_shape,
    resolve_shape,
)
from disco.core.build_platform import (
    BuildComposition,
    BuildPlatformRegistry,
    BuildProfile,
    CapabilityLayer,
    ComponentId,
    ComponentIntent,
    PolicyLayer,
    RegistryError,
    ResolutionError,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]

#: Every file this package adds.  Named explicitly so the rollback assertion is
#: about a closed set rather than a glob that could silently grow.
PKG18_FILES: tuple[str, ...] = (
    "current/packages/core/tests/build_platform_registration_shapes.py",
    "current/packages/core/tests/test_build_platform_mobile_registration.py",
    "current/packages/core/tests/test_build_platform_desktop_registration.py",
    "current/packages/core/tests/test_build_platform_registration_locality.py",
)

#: Modules that spawn or exec a process.  An import of any of these anywhere in
#: the PKG-18 surface would make "no command executed" a claim about restraint;
#: their absence makes it a property of the code.
_EXECUTION_MODULES = frozenset({"subprocess", "pty", "multiprocessing", "asyncio.subprocess"})

#: Attribute calls that execute something without importing ``subprocess``.
_EXECUTION_CALLS = frozenset(
    {
        "os.system",
        "os.popen",
        "os.execv",
        "os.execve",
        "os.execvp",
        "os.execvpe",
        "os.spawnv",
        "os.spawnve",
        "os.posix_spawn",
        "os.fork",
        "os.forkpty",
    }
)

#: Toolchain, signing, simulator and store commands the package acceptance
#: names, plus the two platform families the fixtures are shaped like.
_FORBIDDEN_TOOL_TOKENS: tuple[str, ...] = (
    "xcodebuild",
    "xcrun",
    "xcode",
    "simctl",
    "altool",
    "notarytool",
    "fastlane",
    "cocoapods",
    "adb",
    "gradle",
    "gradlew",
    "sdkmanager",
    "avdmanager",
    "emulator",
    "apksigner",
    "zipalign",
    "bundletool",
    "keytool",
    "jarsigner",
    "expo",
    "eas-cli",
    "react-native",
    "metro",
    "cargo",
    "rustc",
    "rustup",
    "tauri",
    "electron",
    "codesign",
    "signtool",
    "appstoreconnect",
    "playstore",
)

#: Where a central target switch for these shapes would have to live.  This
#: surface must name none of the tokens at all.
_BUILD_PLATFORM_DIR = "current/packages/core/src/disco/core/build_platform"

#: The wider target-neutral Core surface the acceptance bullet protects.
_NEUTRAL_CORE_DIRS: tuple[str, ...] = (
    _BUILD_PLATFORM_DIR,
    "current/packages/core/src/disco/core/loop",
)

#: The token sites that **already existed** at the certified parent
#: `7bb71d9cf8bef44159a333b411d6109eb4a5bb8c`, recorded rather than repaired or
#: excluded.  All three are the loop's generic Rust-workspace bootstrap
#: detector (`_detect_cargo_toml` and its consumers) — a language detector that
#: reports `cargo run --bin <name>` to the model.  It long predates this
#: package, is byte-unchanged by it, and is not a target switch for any
#: registered shape.
#:
#: Freezing the set rather than narrowing the scan is deliberate: a baseline
#: keeps `loop/` under the assertion, so a *new* occurrence anywhere in
#: target-neutral Core still fails, while the pre-existing three are stated out
#: loud instead of quietly excluded.
_PREEXISTING_TOKEN_SITES: frozenset[tuple[str, str]] = frozenset(
    {
        ("current/packages/core/src/disco/core/loop/bootstrap.py", "cargo"),
        ("current/packages/core/src/disco/core/loop/message_rendering.py", "cargo"),
        ("current/packages/core/src/disco/core/loop/phase_gates.py", "cargo"),
    }
)


def _names_token(text: str, token: str) -> bool:
    """Match *token* as a whole name, not as a substring.

    ``\\b`` is wrong here because ``_`` is a word character: it would miss
    ``cargo_path`` (which *is* a target switch) while still matching nothing
    useful.  Bounding on alphanumerics instead catches ``cargo_path`` and
    rejects ``export`` — which contains ``expo`` and is everywhere in Core.
    """
    pattern = rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])"
    return re.search(pattern, text) is not None


def _pkg18_sources() -> list[tuple[str, str]]:
    return [(rel, (_REPO_ROOT / rel).read_text(encoding="utf-8")) for rel in PKG18_FILES]


def _imported_module_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


def _called_dotted_names(tree: ast.AST) -> set[str]:
    return {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}


def _declared_operations(composition: BuildComposition) -> set[str]:
    plan = composition.target_plan
    operations = {intent.operation for intent in composition.construction.intents}
    operations |= {intent.operation for intent in plan.intents}
    operations |= {intent.operation for intent in plan.preview.intents}
    operations |= {check.intent.operation for check in plan.verifier.checks}
    if plan.package is not None:
        operations |= {intent.operation for intent in plan.package.intents}
    return operations


def test_the_pkg18_surface_spawns_no_process_at_all() -> None:
    """The command-inventory proof, as a property rather than a promise.

    If no process can be spawned, then in particular no Xcode, Android SDK,
    emulator, Expo, React Native, Rust, Tauri, Electron, signing, simulator or
    store command was spawned.  That is a stronger statement than auditing a
    list of command names, and it cannot rot as new tool names appear.
    """
    offenders: list[str] = []
    for rel, text in _pkg18_sources():
        tree = ast.parse(text, filename=rel)
        offenders.extend(
            f"{rel}: imports {module}"
            for module in sorted(_imported_module_names(tree) & _EXECUTION_MODULES)
        )
        offenders.extend(
            f"{rel}: calls {call}" for call in sorted(_called_dotted_names(tree) & _EXECUTION_CALLS)
        )
    assert offenders == []


def test_no_declared_intent_operation_names_a_toolchain_or_store_command() -> None:
    """Not one declared effect is a toolchain, signing, simulator or store step."""
    named = [
        f"{operation} names {token}"
        for operation in MOBILE_OPERATIONS + DESKTOP_OPERATIONS
        for token in _FORBIDDEN_TOOL_TOKENS
        if _names_token(operation.casefold(), token)
    ]
    assert named == []

    # And the operations the resolver actually produces are exactly those.
    for profile_id, expected in (
        (MOBILE_PROFILE_ID, MOBILE_OPERATIONS),
        (DESKTOP_PROFILE_ID, DESKTOP_OPERATIONS),
    ):
        assert _declared_operations(resolve_shape(profile_id)) == set(expected)


def test_a_component_intent_carries_no_execution_handle() -> None:
    """An intent is evidence of requested work, never a way to perform it."""
    intent = ComponentIntent(operation="mobile.assemble_bundle")
    assert set(type(intent).model_fields) == {
        "operation",
        "parameters",
        "required_capabilities",
    }
    with pytest.raises(ValueError):
        ComponentIntent.model_validate(
            {"operation": "mobile.assemble_bundle", "command": "adb install"}
        )


def _token_sites(rel_dirs: tuple[str, ...]) -> set[tuple[str, str]]:
    sites: set[tuple[str, str]] = set()
    for rel_dir in rel_dirs:
        for path in sorted((_REPO_ROOT / rel_dir).rglob("*.py")):
            folded = path.read_text(encoding="utf-8").casefold()
            rel = path.relative_to(_REPO_ROOT).as_posix()
            sites.update(
                (rel, token) for token in _FORBIDDEN_TOOL_TOKENS if _names_token(folded, token)
            )
    return sites


def test_the_core_build_platform_surface_names_no_platform_or_toolchain() -> None:
    """No central target switch, where one would actually have to live.

    A registered shape reaches production through the Build Platform contracts,
    registry and resolver.  If either shape had leaked a platform conditional
    into Core, this is the surface it would appear on — and it names none of the
    thirty-two tokens at all.
    """
    assert _token_sites((_BUILD_PLATFORM_DIR,)) == set()


def test_target_neutral_core_gains_no_new_platform_or_toolchain_name() -> None:
    """The wider neutral-Core surface is frozen against *new* occurrences.

    Equality, not emptiness: the three pre-existing `cargo` sites are the loop's
    generic Rust-workspace detector, which this package does not touch.  Stating
    them keeps `loop/` inside the assertion, so adding a platform name anywhere
    in target-neutral Core fails here rather than passing a narrowed scan.
    """
    assert _token_sites(_NEUTRAL_CORE_DIRS) == set(_PREEXISTING_TOKEN_SITES)


def test_the_proof_surface_is_test_owned_so_deleting_it_is_a_complete_rollback() -> None:
    """No shipped source references the proof namespace or its fixture module."""
    referencing: list[str] = []
    for package_src in sorted((_REPO_ROOT / "current" / "packages").glob("*/src")):
        for path in sorted(package_src.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            rel = path.relative_to(_REPO_ROOT).as_posix()
            if "build_platform_registration_shapes" in text:
                referencing.append(f"{rel} imports the fixture module")
            if f'namespace="{PROOF_NAMESPACE}"' in text:
                referencing.append(f"{rel} registers in the proof namespace")
    assert referencing == []

    for rel in PKG18_FILES:
        assert (_REPO_ROOT / rel).is_file()
        assert rel.startswith("current/packages/core/tests/")


def test_unregistering_the_proof_profiles_is_a_clean_rollback() -> None:
    """Removing the adapters leaves resolution failing closed, not degraded."""
    registry = build_registration_registry()
    assert registry.profiles.get(MOBILE_PROFILE_ID) is not None
    assert registry.profiles.get(DESKTOP_PROFILE_ID) is not None

    registry.unregister_profile(MOBILE_PROFILE_ID)
    registry.unregister_profile(DESKTOP_PROFILE_ID)

    assert registry.profiles.get(MOBILE_PROFILE_ID) is None
    assert registry.profiles.get(DESKTOP_PROFILE_ID) is None
    assert registry.profiles.choices() == ()

    # Fails closed: no nearest-name, no fallback to the other registered shape.
    for profile_id in (MOBILE_PROFILE_ID, DESKTOP_PROFILE_ID):
        with pytest.raises(ResolutionError):
            resolve_shape(profile_id, registry=registry)


def test_registration_has_exactly_one_lifecycle_and_no_back_door() -> None:
    """The second-lifecycle negative: every alternative route is refused."""
    registry = register_mobile_shape(BuildPlatformRegistry())

    protected = ComponentId(namespace="disco", name="smuggled_profile", version="1")
    with pytest.raises(RegistryError):
        registry.register_profile(
            BuildProfile(
                id=protected,
                label="smuggled",
                engine=protected,
                target=protected,
                verifier=protected,
                preview=protected,
                capabilities=CapabilityLayer(source=protected.canonical),
                policy=PolicyLayer(source=protected.canonical),
            )
        )

    # A duplicate identity is refused rather than overwriting the first.
    with pytest.raises(RegistryError):
        register_mobile_shape(registry)
