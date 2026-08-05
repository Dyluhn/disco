"""Two synthetic non-web target shapes, registered through the public registry.

**Neither platform is supported.**  There is no Expo, React Native, Tauri or
Electron support in this product, and nothing in this module or the tests that
use it should be read as claiming otherwise.  What these fixtures demonstrate is
narrower and precise: *the ordinary public registry and lifecycle accommodate a
mobile-shaped and a desktop-shaped target without web assumptions.*  Registry
and lifecycle, not platforms.

The two shapes exist to exercise structural properties that a web target would
satisfy accidentally:

* **Opaque artifacts.**  Delivery and package shapes are adapter-owned names
  over an opaque manifest reference.  Core assigns no filename or transport
  meaning to them (see :class:`EntryDescriptor`), so nothing here needs an
  ``index.html``, a served directory, a port, or a URL.
* **Preview is not universal.**  The mobile shape previews *not at all*
  (``modality="none"``) because previewing a mobile app shape would need a
  simulator or a device.  The desktop shape previews **optionally**, as a
  manifest dry run that degrades when unavailable, because previewing it for
  real would need a window.  Between them they cover the ``none``/optional
  requirement without either one ever requiring a display.
* **Verification is target specific.**  Each verifier accepts only
  :attr:`VerificationClaimKind.TARGET_SPECIFIC`, so a browser-runtime claim
  (``http_ready``, ``rendered_content``, …) cannot be attached to one — the
  contract refuses it rather than a convention discouraging it.
* **Unsupported toolchains are said in the existing vocabulary.**  An absent
  Android SDK or Rust toolchain is a PKG-17 :class:`HostCapabilityProbe` whose
  :class:`ProbeObservation` is ``NOT_RUN`` and whose grade is ``UNSUPPORTED``.
  This package invents no second way to say "not available".

**No SDK, toolchain, signing, simulator, device, window or store command runs
anywhere in this module or its tests.**  That is exactly why the toolchain
observations are ``NOT_RUN``: the honest outcome of a probe that was
deliberately never executed.  A :class:`ComponentIntent` is a frozen value
describing requested work — it carries no execution handle, so building a plan
cannot invoke anything.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from disco.core.build_platform import (
    BuildComposition,
    BuildPlatformRegistry,
    BuildProfile,
    CapabilityLayer,
    CapabilitySupport,
    ComponentId,
    ComponentIntent,
    ComponentKind,
    ComponentSpec,
    ConstructionEngineDefinition,
    ConstructionPlan,
    ConstructionRequest,
    DeliveryIntent,
    EntryDescriptor,
    PackageExporterDefinition,
    PackagePlan,
    PackageRequest,
    PolicyLayer,
    PreviewPlan,
    PreviewPolicy,
    PromptContextInputs,
    ReadinessSignal,
    ResolutionInputs,
    TargetAdapterDefinition,
    TargetPlan,
    TargetRequest,
    VerifierCheck,
    VerifierPlan,
    resolve_build_composition,
)
from disco.core.build_platform.host_capabilities import (
    HostCapabilityProbe,
    HostPhase,
    PhaseRequirement,
    ProbeMethod,
    pending_observations,
)
from disco.core.build_platform.host_profiles import (
    HOST_NAMESPACE,
    HostProfile,
    HostProfileDefinition,
)
from disco.core.verification import HostVerificationClaim, VerificationClaimKind

#: The public namespace these proof components register under.  ``disco`` is
#: host-protected, so using it would bypass the public registration path this
#: package exists to exercise.
PROOF_NAMESPACE = "proof"


class BundleVerificationError(AssertionError):
    """A target-specific verifier check rejected the assembled bundle.

    Named rather than a bare ``AssertionError`` so a negative test can assert
    that verification failed *for the declared reason* instead of catching any
    incidental assertion the harness happens to raise.
    """


#: The capabilities a host must grant for either shape to run end to end.  These
#: are ordinary workspace/process capabilities — deliberately the same three the
#: persistent non-web conformance target uses.  Neither shape asks for network
#: egress, a display, or a toolchain.
SHAPE_CAPABILITIES = frozenset({"workspace.read", "workspace.write", "process.execute"})


def _id(name: str) -> ComponentId:
    return ComponentId(namespace=PROOF_NAMESPACE, name=name, version="1")


MOBILE_PROFILE_ID = _id("mobile_profile")
MOBILE_ENGINE_ID = _id("mobile_engine")
MOBILE_TARGET_ID = _id("mobile_target")
MOBILE_VERIFIER_ID = _id("mobile_verifier")
MOBILE_PREVIEW_ID = _id("mobile_preview")
MOBILE_EXPORTER_ID = _id("mobile_exporter")

DESKTOP_PROFILE_ID = _id("desktop_profile")
DESKTOP_ENGINE_ID = _id("desktop_engine")
DESKTOP_TARGET_ID = _id("desktop_target")
DESKTOP_VERIFIER_ID = _id("desktop_verifier")
DESKTOP_PREVIEW_ID = _id("desktop_preview")
DESKTOP_EXPORTER_ID = _id("desktop_exporter")

#: Every operation either shape can request, in lifecycle order.  The tests
#: assert the executed sequence equals this, which is what makes an
#: unmediated/unknown effect a test failure rather than a silent extra step.
MOBILE_OPERATIONS: tuple[str, ...] = (
    "mobile.author_manifest",
    "mobile.assemble_bundle",
    "mobile.verify_bundle",
    "mobile.package_archive",
)
DESKTOP_OPERATIONS: tuple[str, ...] = (
    "desktop.author_manifest",
    "desktop.assemble_bundle",
    "desktop.preview_manifest",
    "desktop.verify_bundle",
    "desktop.package_archive",
)


def _capabilities(source: str) -> CapabilityLayer:
    return CapabilityLayer(source=source, allowed=SHAPE_CAPABILITIES)


def _spec(component_id: ComponentId, kind: ComponentKind) -> ComponentSpec:
    return ComponentSpec(
        id=component_id,
        kind=kind,
        capabilities=_capabilities(component_id.canonical),
        policy=PolicyLayer(source=component_id.canonical),
    )


# --------------------------------------------------------------------------
# Mobile-shaped target (Expo/React-Native-*shaped*; the platform is NOT supported)
# --------------------------------------------------------------------------


class MobileShapeEngine:
    """Authors an opaque bundle manifest.  Runs no packager and no SDK."""

    @property
    def id(self) -> ComponentId:
        return MOBILE_ENGINE_ID

    def plan(self, request: ConstructionRequest) -> ConstructionPlan:
        return ConstructionPlan(
            engine=self.id,
            intents=(
                ComponentIntent(
                    operation="mobile.author_manifest",
                    required_capabilities=frozenset({"workspace.write"}),
                ),
            ),
            required_capabilities=frozenset({"workspace.write"}),
        )


class MobileShapeAdapter:
    """A mobile-shaped target whose preview modality is ``none``.

    ``none`` is the honest modality: previewing a mobile app shape needs a
    simulator or a device, and this package runs neither.  The contract makes
    that structural rather than conventional — :class:`PreviewPlan` refuses a
    ``none`` modality that also declares entry or readiness work, so a "preview
    nothing, but here is how to preview it" plan is unrepresentable.
    """

    @property
    def id(self) -> ComponentId:
        return MOBILE_TARGET_ID

    def plan(self, request: TargetRequest) -> TargetPlan:
        entry = EntryDescriptor(
            kind="bundle_manifest",
            reference="dist/mobile/app.bundle.manifest",
        )
        return TargetPlan(
            target=self.id,
            intents=(
                ComponentIntent(
                    operation="mobile.assemble_bundle",
                    required_capabilities=frozenset({"workspace.read", "workspace.write"}),
                ),
            ),
            delivery=DeliveryIntent(shape="bundle.mobile_app", entry=entry),
            preview=PreviewPlan(
                modality="none",
                policy=PreviewPolicy(required=False, unavailable="degrade"),
            ),
            verifier=VerifierPlan(
                checks=(
                    VerifierCheck(
                        check_id="mobile_bundle_contract",
                        issuer=MOBILE_VERIFIER_ID,
                        receipt_kind="proof.mobile_bundle@1",
                        intent=ComponentIntent(
                            operation="mobile.verify_bundle",
                            required_capabilities=frozenset({"workspace.read"}),
                        ),
                        accepted_claim_kinds=frozenset({VerificationClaimKind.TARGET_SPECIFIC}),
                        claims=(
                            HostVerificationClaim(
                                claim_id="mobile.bundle_contract",
                                kind=VerificationClaimKind.TARGET_SPECIFIC,
                                expected=(
                                    "the bundle manifest lists every declared asset "
                                    "and its recorded digest"
                                ),
                                source_authority=MOBILE_TARGET_ID.canonical,
                            ),
                        ),
                    ),
                )
            ),
            package=PackagePlan(
                package_shape="bundle.mobile_archive",
                intents=(
                    ComponentIntent(
                        operation="mobile.package_archive",
                        required_capabilities=frozenset({"workspace.read"}),
                    ),
                ),
            ),
            required_capabilities=frozenset({"workspace.read", "workspace.write"}),
        )


class MobileShapeExporter:
    @property
    def id(self) -> ComponentId:
        return MOBILE_EXPORTER_ID

    def plan(self, request: PackageRequest) -> PackagePlan:
        return PackagePlan(
            package_shape="bundle.mobile_archive",
            intents=(
                ComponentIntent(
                    operation="mobile.package_archive",
                    required_capabilities=frozenset({"workspace.read"}),
                ),
            ),
        )


# --------------------------------------------------------------------------
# Desktop-shaped target (Tauri/Electron-*shaped*; the platform is NOT supported)
# --------------------------------------------------------------------------


class DesktopShapeEngine:
    """Authors an opaque bundle manifest.  Compiles nothing and links nothing."""

    @property
    def id(self) -> ComponentId:
        return DESKTOP_ENGINE_ID

    def plan(self, request: ConstructionRequest) -> ConstructionPlan:
        return ConstructionPlan(
            engine=self.id,
            intents=(
                ComponentIntent(
                    operation="desktop.author_manifest",
                    required_capabilities=frozenset({"workspace.write"}),
                ),
            ),
            required_capabilities=frozenset({"workspace.write"}),
        )


class DesktopShapeAdapter:
    """A desktop-shaped target whose preview is optional and opens no window.

    The preview is a *manifest dry run*: it reads the assembled manifest and
    reports readiness by exit status.  It requires ``process.execute`` and
    nothing else — in particular it does not require ``display.interactive``,
    which is what keeps it from being a window.  Its policy is
    ``required=False, unavailable="degrade"``, so a host without the capability
    degrades preview alone and every other phase proceeds.
    """

    @property
    def id(self) -> ComponentId:
        return DESKTOP_TARGET_ID

    def plan(self, request: TargetRequest) -> TargetPlan:
        entry = EntryDescriptor(
            kind="bundle_manifest",
            reference="dist/desktop/app.bundle.manifest",
        )
        return TargetPlan(
            target=self.id,
            intents=(
                ComponentIntent(
                    operation="desktop.assemble_bundle",
                    required_capabilities=frozenset({"workspace.read", "workspace.write"}),
                ),
            ),
            delivery=DeliveryIntent(shape="bundle.desktop_app", entry=entry),
            preview=PreviewPlan(
                modality="manifest_dry_run",
                entry=entry,
                readiness=(ReadinessSignal(kind="exit_zero"),),
                intents=(
                    ComponentIntent(
                        operation="desktop.preview_manifest",
                        required_capabilities=frozenset({"process.execute"}),
                    ),
                ),
                required_capabilities=frozenset({"process.execute"}),
                policy=PreviewPolicy(required=False, unavailable="degrade"),
            ),
            verifier=VerifierPlan(
                checks=(
                    VerifierCheck(
                        check_id="desktop_bundle_contract",
                        issuer=DESKTOP_VERIFIER_ID,
                        receipt_kind="proof.desktop_bundle@1",
                        intent=ComponentIntent(
                            operation="desktop.verify_bundle",
                            required_capabilities=frozenset({"workspace.read"}),
                        ),
                        accepted_claim_kinds=frozenset({VerificationClaimKind.TARGET_SPECIFIC}),
                        claims=(
                            HostVerificationClaim(
                                claim_id="desktop.bundle_contract",
                                kind=VerificationClaimKind.TARGET_SPECIFIC,
                                expected=(
                                    "the bundle manifest names the declared entry and "
                                    "its recorded digest"
                                ),
                                source_authority=DESKTOP_TARGET_ID.canonical,
                            ),
                        ),
                    ),
                )
            ),
            package=PackagePlan(
                package_shape="bundle.desktop_archive",
                intents=(
                    ComponentIntent(
                        operation="desktop.package_archive",
                        required_capabilities=frozenset({"workspace.read"}),
                    ),
                ),
            ),
            required_capabilities=frozenset({"workspace.read", "workspace.write"}),
        )


class DesktopShapeExporter:
    @property
    def id(self) -> ComponentId:
        return DESKTOP_EXPORTER_ID

    def plan(self, request: PackageRequest) -> PackagePlan:
        return PackagePlan(
            package_shape="bundle.desktop_archive",
            intents=(
                ComponentIntent(
                    operation="desktop.package_archive",
                    required_capabilities=frozenset({"workspace.read"}),
                ),
            ),
        )


# --------------------------------------------------------------------------
# Registration through the ordinary public registry
# --------------------------------------------------------------------------


def _register_shape(
    registry: BuildPlatformRegistry,
    *,
    profile_id: ComponentId,
    label: str,
    engine: MobileShapeEngine | DesktopShapeEngine,
    target: MobileShapeAdapter | DesktopShapeAdapter,
    exporter: MobileShapeExporter | DesktopShapeExporter,
    verifier_id: ComponentId,
    preview_id: ComponentId,
) -> None:
    """Register one shape using only public ``register_*`` entry points.

    Nothing here reaches a private catalog method, a built-in helper, or a
    central target switch.  This is the same lifecycle any external component
    would use, which is the point of the package.
    """
    engine_plan = engine.plan(ConstructionRequest(profile=profile_id, goal=label))
    registry.register_engine(
        _spec(engine.id, ComponentKind.ENGINE),
        ConstructionEngineDefinition(id=engine.id, plan=engine_plan),
    )
    target_plan = target.plan(TargetRequest(profile=profile_id, engine=engine.id, goal=label))
    registry.register_target(
        _spec(target.id, ComponentKind.TARGET),
        TargetAdapterDefinition(id=target.id, plan=target_plan),
    )
    exporter_plan = exporter.plan(
        PackageRequest(
            profile=profile_id,
            target=target.id,
            delivery=target_plan.delivery,
            revision_ref="proof:unbound",
        )
    )
    registry.register_exporter(
        _spec(exporter.id, ComponentKind.EXPORTER),
        PackageExporterDefinition(id=exporter.id, plan=exporter_plan),
    )
    registry.register_component(_spec(verifier_id, ComponentKind.VERIFIER))
    registry.register_component(_spec(preview_id, ComponentKind.PREVIEW))
    registry.register_profile(
        BuildProfile(
            id=profile_id,
            label=label,
            engine=engine.id,
            target=target.id,
            verifier=verifier_id,
            preview=preview_id,
            exporter=exporter.id,
            compatible_reference_categories=frozenset({"reference_pack"}),
            capabilities=_capabilities(profile_id.canonical),
            policy=PolicyLayer(source=profile_id.canonical),
        )
    )


def register_mobile_shape(registry: BuildPlatformRegistry) -> BuildPlatformRegistry:
    _register_shape(
        registry,
        profile_id=MOBILE_PROFILE_ID,
        label="Mobile-shaped registration proof (platform NOT supported)",
        engine=MobileShapeEngine(),
        target=MobileShapeAdapter(),
        exporter=MobileShapeExporter(),
        verifier_id=MOBILE_VERIFIER_ID,
        preview_id=MOBILE_PREVIEW_ID,
    )
    return registry


def register_desktop_shape(registry: BuildPlatformRegistry) -> BuildPlatformRegistry:
    _register_shape(
        registry,
        profile_id=DESKTOP_PROFILE_ID,
        label="Desktop-shaped registration proof (platform NOT supported)",
        engine=DesktopShapeEngine(),
        target=DesktopShapeAdapter(),
        exporter=DesktopShapeExporter(),
        verifier_id=DESKTOP_VERIFIER_ID,
        preview_id=DESKTOP_PREVIEW_ID,
    )
    return registry


def register_variant(
    registry: BuildPlatformRegistry,
    *,
    variant: str,
    target_plan_for: Callable[[ComponentId], TargetPlan],
) -> ComponentId:
    """Register a deliberately-defective variant and return its profile ID.

    Negative tests use this so a refusal can be driven through the *real*
    resolver rather than asserted against a value object.  Each variant differs
    from a shipped shape in exactly one declared property, which is what lets a
    blocked result be attributed to that property instead of to the variant
    being different in general.
    """
    profile_id = _id(f"{variant}_profile")
    engine_id = _id(f"{variant}_engine")
    target_id = _id(f"{variant}_target")
    verifier_id = _id(f"{variant}_verifier")
    preview_id = _id(f"{variant}_preview")

    registry.register_engine(
        _spec(engine_id, ComponentKind.ENGINE),
        ConstructionEngineDefinition(
            id=engine_id,
            plan=ConstructionPlan(engine=engine_id, required_capabilities=frozenset()),
        ),
    )
    registry.register_target(
        _spec(target_id, ComponentKind.TARGET),
        TargetAdapterDefinition(id=target_id, plan=target_plan_for(target_id)),
    )
    registry.register_component(_spec(verifier_id, ComponentKind.VERIFIER))
    registry.register_component(_spec(preview_id, ComponentKind.PREVIEW))
    registry.register_profile(
        BuildProfile(
            id=profile_id,
            label=f"Variant control: {variant}",
            engine=engine_id,
            target=target_id,
            verifier=verifier_id,
            preview=preview_id,
            capabilities=_capabilities(profile_id.canonical),
            policy=PolicyLayer(source=profile_id.canonical),
        )
    )
    return profile_id


def build_registration_registry() -> BuildPlatformRegistry:
    """Both shapes in one registry, proving they coexist without collision."""
    registry = BuildPlatformRegistry()
    register_mobile_shape(registry)
    register_desktop_shape(registry)
    return registry


def resolve_shape(
    profile_id: ComponentId,
    *,
    host_capabilities: frozenset[str] = SHAPE_CAPABILITIES,
    host_support: tuple[CapabilitySupport, ...] = (),
    registry: BuildPlatformRegistry | None = None,
) -> BuildComposition:
    """Drive one registered shape through the real production resolver."""
    ceiling = _capabilities("ceiling")
    return resolve_build_composition(
        registry if registry is not None else build_registration_registry(),
        ResolutionInputs(
            profile=profile_id,
            goal="assemble and package an opaque application bundle",
            platform_capabilities=ceiling.model_copy(update={"source": "platform"}),
            user_capabilities=ceiling.model_copy(update={"source": "user"}),
            host_capabilities=CapabilityLayer(source="host", allowed=host_capabilities),
            host_support=host_support,
            platform_policy=PolicyLayer(source="platform"),
            user_policy=PolicyLayer(source="user"),
            prompt_context=PromptContextInputs(),
        ),
    )


# --------------------------------------------------------------------------
# Unsupported toolchains, in PKG-17's existing vocabulary
# --------------------------------------------------------------------------

#: The toolchain capability each shape *would* need if it actually built the
#: platform it is shaped like.  Named here only so the absence can be stated
#: honestly; nothing in either shape's lifecycle requires them.
MOBILE_TOOLCHAIN_CAPABILITY = "toolchain.android_sdk"
DESKTOP_TOOLCHAIN_CAPABILITY = "toolchain.rust_cargo"

#: Why every toolchain observation below is ``NOT_RUN``.  This is not a
#: placeholder: the package acceptance forbids executing any SDK, toolchain,
#: signing, simulator or store command, so "deliberately not executed" is the
#: true outcome and ``NOT_RUN`` is the outcome that says it.
TOOLCHAIN_NOT_RUN_DETAIL = (
    "probe deliberately not executed: this package runs no SDK or toolchain command"
)


def _toolchain_probe(probe_id: str, capability: str, subject: str) -> HostCapabilityProbe:
    return HostCapabilityProbe(
        probe_id=probe_id,
        capability=capability,
        method=ProbeMethod.TOOLCHAIN_VERSION,
        subject=subject,
        owner="packages/core/tests/build_platform_registration_shapes.py",
        phases=frozenset({HostPhase.PACKAGE}),
    )


MOBILE_TOOLCHAIN_HOST = HostProfileDefinition(
    id=ComponentId(namespace=HOST_NAMESPACE, name="mobile_toolchain_proof", version="1"),
    os_label="unevidenced",
    summary="Mobile toolchain proof host; no toolchain is claimed and none is probed",
    probes=(
        _toolchain_probe(
            "proof.toolchain_android_sdk",
            MOBILE_TOOLCHAIN_CAPABILITY,
            "an Android SDK build-tools installation",
        ),
    ),
    phase_requirements=(
        PhaseRequirement(
            phase=HostPhase.PACKAGE,
            capabilities=frozenset({MOBILE_TOOLCHAIN_CAPABILITY}),
        ),
    ),
)

DESKTOP_TOOLCHAIN_HOST = HostProfileDefinition(
    id=ComponentId(namespace=HOST_NAMESPACE, name="desktop_toolchain_proof", version="1"),
    os_label="unevidenced",
    summary="Desktop toolchain proof host; no toolchain is claimed and none is probed",
    probes=(
        _toolchain_probe(
            "proof.toolchain_rust_cargo",
            DESKTOP_TOOLCHAIN_CAPABILITY,
            "a Rust cargo toolchain",
        ),
    ),
    phase_requirements=(
        PhaseRequirement(
            phase=HostPhase.PACKAGE,
            capabilities=frozenset({DESKTOP_TOOLCHAIN_CAPABILITY}),
        ),
    ),
)


class ShapeHost:
    """The test-owned effect mediator for both shapes.

    This is the *only* place a declared intent turns into an effect, and every
    effect it performs is a workspace file operation.  An operation it does not
    recognise raises rather than being ignored, so a shape cannot grow a silent
    extra step — and no branch of it shells out, so no SDK, signing, simulator
    or store command can run through this path.

    Its whole state is the directory it is handed plus three attributes.
    Discarding the instance and the ``tmp_path`` is what "removing test-owned
    state" means for this package.
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.executed: list[str] = []
        self.preview_result: str | None = None
        self.package_digest: str | None = None

    def _bundle_dir(self, platform: str) -> Path:
        bundle = self.workspace / "dist" / platform
        bundle.mkdir(parents=True, exist_ok=True)
        return bundle

    def execute(self, intent: ComponentIntent) -> None:
        self.executed.append(intent.operation)
        platform, _, verb = intent.operation.partition(".")
        bundle = self._bundle_dir(platform)
        if verb == "author_manifest":
            (bundle / "app.spec.json").write_text(
                json.dumps({"shape": platform, "assets": ["app.payload"]}),
                encoding="utf-8",
            )
        elif verb == "assemble_bundle":
            spec = json.loads((bundle / "app.spec.json").read_text(encoding="utf-8"))
            payload = f"{platform}:{','.join(spec['assets'])}".encode()
            (bundle / "app.payload").write_bytes(payload)
            (bundle / "app.bundle.manifest").write_text(
                json.dumps(
                    {
                        "shape": platform,
                        "entry": "app.payload",
                        "digest": f"sha256:{hashlib.sha256(payload).hexdigest()}",
                    }
                ),
                encoding="utf-8",
            )
        elif verb == "preview_manifest":
            manifest = json.loads((bundle / "app.bundle.manifest").read_text(encoding="utf-8"))
            self.preview_result = manifest["digest"]
        elif verb == "verify_bundle":
            manifest = json.loads((bundle / "app.bundle.manifest").read_text(encoding="utf-8"))
            payload = (bundle / manifest["entry"]).read_bytes()
            expected = f"sha256:{hashlib.sha256(payload).hexdigest()}"
            if manifest["digest"] != expected:
                raise BundleVerificationError(
                    f"{platform} bundle digest {manifest['digest']} does not match {expected}"
                )
        elif verb == "package_archive":
            payload = b"\n".join(
                path.read_bytes() for path in sorted(bundle.iterdir()) if path.is_file()
            )
            self.package_digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
        else:  # pragma: no cover - makes the host fail closed on a new effect
            raise AssertionError(f"unmediated/unknown intent: {intent.operation}")


def run_lifecycle(composition: BuildComposition, host: ShapeHost) -> None:
    """Drive one composition's declared phases through the mediator, in order.

    Preview is driven from ``preview.intents``, which is empty for a ``none``
    modality — so a shape that previews not at all needs no special case here.
    """
    for intent in composition.construction.intents:
        host.execute(intent)
    for intent in composition.target_plan.intents:
        host.execute(intent)
    for intent in composition.target_plan.preview.intents:
        host.execute(intent)
    for check in composition.target_plan.verifier.checks:
        host.execute(check.intent)
    if composition.target_plan.package is not None:
        for intent in composition.target_plan.package.intents:
            host.execute(intent)


def unevidenced_toolchain_profile(definition: HostProfileDefinition) -> HostProfile:
    """Bind a toolchain definition entirely from ``NOT_RUN`` observations.

    The result advertises nothing, because :class:`HostCapabilityResult` refuses
    an advertising grade without a ``PRESENT`` observation.  That refusal is the
    guard doing the work — this function cannot produce a supported toolchain
    claim even if a future caller wanted one.
    """
    return definition.bind(
        pending_observations(
            definition.probes,
            profile_id=definition.id.canonical,
            detail=TOOLCHAIN_NOT_RUN_DETAIL,
        )
    )
