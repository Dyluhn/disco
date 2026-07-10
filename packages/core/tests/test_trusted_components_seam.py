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


def test_seam_is_fail_closed_nothing_advertises_it() -> None:
    """No tool named anything trusted-component-ish exists in the default
    registry — the tier CANNOT be reached by a model until v0.2 wires it
    deliberately. If this fails, someone registered a tool without the
    design doc's §4 verification checks: stop and read the doc."""
    from disco.tools import build_default_registry

    names = set(build_default_registry().names())
    assert not any("trusted" in n or "component" in n for n in names), names
