"""The trusted-components FAIL-CLOSED seam (v0.2 parking, design doc
docs/trusted-components-design.md): the manifest contract parses its own
design-doc example, and NOTHING advertises the tier — a stub that runs, per
the stubs-and-broken-things discipline (a contract nobody executes is rot)."""

from __future__ import annotations

from disco.core.trusted_components import TrustedComponentManifest


def test_manifest_contract_parses_the_design_doc_example() -> None:
    m = TrustedComponentManifest.model_validate(
        {
            "name": "auth-kit",
            "version": "1.0.0",
            "kind": "trusted_component",
            "summary": "Session auth: login/logout routes, middleware, session store.",
            "when_to_use": "ANY app with accounts, logins, or per-user data.",
            "files": {
                "core/auth.ts": "sha256:" + "a" * 64,
                "core/middleware.ts": "sha256:" + "b" * 64,
                "core/routes.ts": "sha256:" + "c" * 64,
                "core/schema.sql": "sha256:" + "d" * 64,
            },
            "config_surface": ["config/auth.config.ts"],
            "requires": ["database-kit>=1.0"],
            "provides": ["auth"],
            "mounts": {"routes_prefix": "/auth", "middleware": "core/middleware.ts"},
            "probe": "probe/probe.py",
            "guide": "GUIDE.md",
        }
    )
    assert m.name == "auth-kit" and m.requires == ["database-kit>=1.0"]


def test_tools_advertised_only_in_freeform_scope() -> None:
    """WO-TC2 INVERTED the original fail-closed tripwire: the tools are now
    deliberately registered — but ONLY the free-form Build scope (AGENT_TOOLS)
    may carry them (D8). Strict AppKit, artifact, and research scopes must
    never see the names."""
    import inspect

    import disco.tools.appkit_scope as appkit_scope_mod
    from disco.core.trusted_components.registry import TrustedComponentRegistry
    from disco.tools import build_default_registry
    from disco.tools.registry import AGENT_TOOLS, ARTIFACT_TOOLS, RESEARCH_TOOLS

    tc_names = {"add_trusted_component", "eject_trusted_component"}
    # Registration is gated on the registry actually shipping components — an
    # installable-nothing tool is a false affordance. Present IFF non-empty.
    ships_components = bool(TrustedComponentRegistry.default().names())
    registered = tc_names <= set(build_default_registry().names())
    assert registered == ships_components, (
        f"tools registered={registered} but registry ships={ships_components}"
    )
    assert tc_names <= AGENT_TOOLS
    assert tc_names.isdisjoint(RESEARCH_TOOLS)
    assert tc_names.isdisjoint(ARTIFACT_TOOLS)
    # The strict AppKit allowlists are explicit name sets — source-level guard
    # that nobody quietly adds the names to a strict-mode phase set.
    src = inspect.getsource(appkit_scope_mod)
    assert "add_trusted_component" not in src and "eject_trusted_component" not in src
