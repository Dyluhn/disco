"""R1 (GAP G02) — the command/credential boundary, at the core grammar + emitter.

Closeout remediation R1 hardens the release command grammar so a bare POSITIONAL
literal credential can no longer ride a start/build command into the exported
bundle — the hole the frozen ``test_g02_positional_credential`` pins at the public
boundary. This file is the CORE-level companion: it proves the two principled rails
of the detector (``looks_like_credential_literal``) and the operand-ROLE rail
(``check_credential_config_role``), that ``check_declaration_argv`` now rejects a
credential in a leading/middle/trailing positional slot and in a ``config set`` form
while KEEPING every benign supported command accepted, and that the emitter refuses
to lower a credential-bearing spec even if one reached it (defense in depth).

Principled, not a value list (plan §4): the detector is asserted TRUE for several
DIFFERENT credential SHAPE families (npm / GitHub / AWS / JWT / generic
high-entropy) and FALSE for the benign operand set real commands use — so it is a
shape/role detector, never a single-sentinel matcher. No fixture credential VALUE is
ever interpolated into an assertion message (a rejected value can itself be a
secret); rejections are asserted value-free.
"""

from __future__ import annotations

import base64
import os
import secrets

import pytest
from disco.core.release.command_grammar import (
    check_credential_config_role,
    check_declaration_argv,
    check_no_positional_credential,
    looks_like_credential_literal,
)
from disco.core.release.local_compose import emit_local_compose
from disco.core.release.spec import (
    DetectorProvenance,
    EnvScope,
    EnvVarDecl,
    LocalResourceProfile,
    ReleaseAssessment,
    ReleaseService,
    ReleaseSpec,
    ResourceDecl,
    ResourceKind,
    ResourceProfiles,
    RuntimeStrategy,
    ServiceRole,
)

_DIGEST = "a" * 64

# Credential VALUES by SHAPE FAMILY — one per family, so the detector is proven to
# recognize a taxonomy of formats, not a single planted sentinel. None of these is
# named anywhere in production (no deny-list); they are matched by structure only.
_CREDENTIALS_BY_FAMILY: dict[str, str] = {
    "npm": "npm_R1BOUNDARY0aK7bQ2xR9mL4wZ8vT1nH6pJ3cF5dS",
    "github_classic": "ghp_16C7e42F292c6912E7710c838347Ae178B4a01",
    "github_fine": "github_pat_11ABCDE0Y0abcdefghij_klmnopqrstuvwxyz1234567890ABCDwxyz",
    "openai_anthropic": "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789ABCD",
    "slack": "xoxb-2431257534-2431257534-abcdefghijklmnop",
    "aws": "AKIAIOSFODNN7EXAMPLE",
    "google": "AIzaSyA1234567890abcdefghijklmnopqrstuvw",
    "gitlab": "glpat-abcdefghij1234567890",
    "stripe": "sk_live_abcdefghijklmnopqrstuvwx",
    "digitalocean": "dop_v1_" + "a" * 64,
    "sendgrid": "SG.abcdefghijklmnopqrst.uvwxyzABCDEFGHIJKLMNOP",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
    "generic_high_entropy": "wZq3Xf8Kd0Lp2Rn7Tv1Bh4Mj6Yc9Se5Ag8Uw2Qe4Rt6",
    # Recovered families/tiers (the generic-rail narrowing had lost these): the vendor
    # families that carry `-`/`_`, plus the base64url / separator-bearing opaque class.
    "huggingface": "hf_abcdefghijklmnopqrstuvwxyz012345ABCD",
    "shopify": "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",
    "slack_app": "xapp-1-A04ABCDEFGH-1234567890123-abcdefghijklmnop",
    "age_secret": "AGE-SECRET-KEY-1" + "QG9V" * 15,
    "age_recipient": "age1" + "ql3z9k7" * 8,
    # base64url `token_urlsafe`-style + a separator-bearing opaque blob (Tier B).
    "base64url": "kJ8-x_L2mNoPqRs4TuVwXyZ0aB1cD2eF3gH4iJ5kL6m",
    "separator_opaque": "aaMKv6yUcVXK-bp2mhM0gOBe-mkggrlOI-VpAUuJpgJL",
}

# The benign operands real supported commands use — every one must be classified as
# NON-credential so the detector is not a blanket "long string" rejecter. Includes the
# exact operands the frozen + C4/C5 matrices accept.
_BENIGN_OPERANDS: tuple[str, ...] = (
    "server.js",
    "worker.js",
    "node",
    "npm",
    "npx",
    "yarn",
    "pnpm",
    "config",
    "set",
    "//registry.example/:_authToken",  # a path/key with '/', not a credential blob
    "main:app",
    "dist/server.js",
    "packages/service/dist/server.bundle.js",  # a high-entropy-LOOKING but legit path
    "main:application_factory_entrypoint_v2",  # a high-entropy-LOOKING but legit module
    "acme-records-team_members-1ef23532",  # a D1 database_name (kebab/snake identifier)
    "customer-orders-2026-q3-shard-01",  # a kebab identifier: words + short numbers
    "user_sessions_write_ahead_log_table",  # a snake_case identifier
    "alpha-bravo-charlie-delta-echo-foxtrot-golf-hotel",  # word-rich id above the H floor
    "prod-api-gateway-eu-west-1-replica-03",  # a service slug (readable words + numbers)
    "python3.12",
    "http.server",
    "0.0.0.0",
    "uvicorn",
    "gunicorn",
    "${PORT}",
    "${API_TOKEN}",
    "cf:dev",
    "start",
    "run",
    "build",
    "head",
    "upgrade",
    "apply",
    "migrate",
)


# ---- detector unit: SHAPE rail is a taxonomy, not a sentinel -------------------


@pytest.mark.parametrize("family", sorted(_CREDENTIALS_BY_FAMILY))
def test_looks_like_credential_literal_true_for_each_shape_family(family: str) -> None:
    """``looks_like_credential_literal`` is TRUE for a value of EACH credential shape
    family (npm / GitHub / OpenAI-Anthropic / Slack / AWS / Google / GitLab / Stripe /
    DigitalOcean / SendGrid / JWT / generic high-entropy) — proving it recognizes a
    FORMAT TAXONOMY, not one planted sentinel."""
    assert looks_like_credential_literal(_CREDENTIALS_BY_FAMILY[family]) is True, (
        f"the {family} credential shape was not recognized as credential material; the "
        "detector must catch a family by structure, not a specific value."
    )


@pytest.mark.parametrize("operand", _BENIGN_OPERANDS)
def test_looks_like_credential_literal_false_for_benign_operands(operand: str) -> None:
    """``looks_like_credential_literal`` is FALSE for every benign operand real
    commands use (scripts, modules, subcommands, versioned interpreters, whole
    ``${NAME}`` references, and a registry KEY with a '/') — so a supported command is
    never false-rejected."""
    assert looks_like_credential_literal(operand) is False, (
        f"a benign operand {operand!r} was misclassified as a credential; the detector "
        "must not reject a normal script/module/subcommand/path/reference."
    )


def test_whole_env_reference_is_never_credential() -> None:
    """A whole ``${NAME}`` reference — the sanctioned way to carry a secret — is NEVER
    flagged, even when the NAME itself is secret-shaped."""
    for ref in ("${NPM_TOKEN}", "${API_SECRET}", "$AWS_SECRET_ACCESS_KEY"):
        assert looks_like_credential_literal(ref) is False


def test_generic_rail_catches_live_base64url_secrets() -> None:
    """The Tier-B generic rail catches the ENTIRE base64url class STRUCTURALLY — proven
    with freshly-generated ``secrets.token_urlsafe`` / ``urlsafe_b64encode`` tokens
    (never fixed sentinels), so it is a shape detector, not a value match. This is the
    class the earlier `-`/`_`-exclusion regression had silently stopped catching."""
    for _ in range(50):
        for token in (
            secrets.token_urlsafe(32),
            secrets.token_urlsafe(48),
            base64.urlsafe_b64encode(os.urandom(33)).decode().rstrip("="),
        ):
            assert looks_like_credential_literal(token) is True, (
                "a live base64url secret was not recognized as credential material"
            )


def test_generic_rail_spares_near_threshold_kebab_snake_identifiers() -> None:
    """The Tier-B rail spares human-readable kebab/snake identifiers — including
    word-rich ones whose entropy sits ABOVE the base entropy floor — because the
    wordlike-structure guard (not a bare thin-margin threshold) keeps them accepted."""
    for identifier in (
        "acme-records-team_members-1ef23532",
        "customer-orders-2026-q3-shard-01",
        "alpha-bravo-charlie-delta-echo-foxtrot-golf-hotel",
        "prod-api-gateway-eu-west-1-replica-03",
        "user_sessions_write_ahead_log_table",
        "release-candidate-2026-07-14-build-1729",
    ):
        assert looks_like_credential_literal(identifier) is False, (
            f"a benign kebab/snake identifier {identifier!r} was misclassified as a secret"
        )


# ---- check_declaration_argv: positional-credential + role rails ---------------

_DECLARED = frozenset({"PORT", "NPM_TOKEN", "API_TOKEN"})

# start commands whose SOLE defect is a bare POSITIONAL literal credential (leading /
# middle / trailing operand slot) — each must now be rejected value-free. The command
# value is resolved as a LOCAL (never a parametrize id) so no credential reaches JUnit.
_POSITIONAL_CASES: dict[str, list[str]] = {
    "leading": ["node", _CREDENTIALS_BY_FAMILY["npm"], "server.js"],
    "middle": ["node", "server.js", _CREDENTIALS_BY_FAMILY["github_classic"], "worker.js"],
    "trailing_config_set_shape": [
        "npm",
        "config",
        "set",
        "//registry.example/:_authToken",
        _CREDENTIALS_BY_FAMILY["npm"],
    ],
    "generic_blob_positional": [
        "node",
        _CREDENTIALS_BY_FAMILY["generic_high_entropy"],
        "server.js",
    ],
}


@pytest.mark.parametrize("case_id", sorted(_POSITIONAL_CASES))
def test_declaration_argv_rejects_positional_credential(case_id: str) -> None:
    """``check_declaration_argv`` rejects a bare positional literal credential in the
    leading / middle / trailing operand slot (and a generic high-entropy blob), with a
    value-free ``ValueError``. RED before R1 (any non-flag operand was accepted)."""
    argv = tuple(_POSITIONAL_CASES[case_id])
    with pytest.raises(ValueError) as exc:
        check_declaration_argv(argv, declared_names=_DECLARED, field="start_cmd")
    message = str(exc.value)
    for family_value in _CREDENTIALS_BY_FAMILY.values():
        assert family_value not in message, "a rejection must never echo the offending value"


def test_declaration_argv_role_rail_rejects_literal_config_value_even_when_not_shaped() -> None:
    """The operand-ROLE rail catches a ``config set <credential-key> <value>`` where the
    VALUE is not itself a recognized secret shape — proving the role rail is
    independent of the shape rail. ``//host/:_authToken`` is credential-shaped BY KEY,
    so an inline literal value (not a ``${NAME}`` reference) is rejected."""
    argv = ("npm", "config", "set", "//registry.example/:_authToken", "plainshortvalue123")
    # sanity: the value alone is NOT shape-detectable, so the shape rail would miss it
    assert looks_like_credential_literal("plainshortvalue123") is False
    with pytest.raises(ValueError):
        check_declaration_argv(argv, declared_names=_DECLARED, field="start_cmd")


def test_role_rail_accepts_declared_reference_and_ignores_non_credential_key() -> None:
    """A ``config set <credential-key> ${DECLARED}`` reference is accepted; a
    ``config set <non-credential-key> <literal>`` is not a credential form and is left
    alone by the role rail."""
    # declared ${NAME} reference under a credential key -> allowed by the role rail
    check_credential_config_role(
        ("npm", "config", "set", "//registry.example/:_authToken", "${NPM_TOKEN}"),
        declared_names=_DECLARED,
        field="start_cmd",
    )
    # a non-credential key (e.g. `registry`) with a literal value is not a credential form
    check_credential_config_role(
        ("npm", "config", "set", "registry", "example.com"),
        declared_names=_DECLARED,
        field="start_cmd",
    )


# ---- positive: benign supported commands keep EXACT argv + stay accepted -------

_ACCEPTED_COMMANDS: dict[str, list[str]] = {
    "node_script": ["node", "server.js"],
    "npm_start": ["npm", "start"],
    "uvicorn_module": ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "${PORT}"],
    "python_module": ["python", "-m", "http.server"],
    "flag_ref": ["node", "srv", "--host", "${PORT}"],
    "credential_as_declared_ref": ["node", "srv", "--token", "${API_TOKEN}"],
    "config_set_declared_ref": [
        "npm",
        "config",
        "set",
        "//registry.example/:_authToken",
        "${NPM_TOKEN}",
    ],
    "high_entropy_module_path": ["node", "packages/service/dist/server.bundle.js"],
}


@pytest.mark.parametrize("case_id", sorted(_ACCEPTED_COMMANDS))
def test_declaration_argv_accepts_benign_commands(case_id: str) -> None:
    """Every benign supported command stays ACCEPTED with EXACT argv semantics — a
    credential passed correctly as a whole declared ``${NAME}`` reference is accepted,
    and a high-entropy-LOOKING legitimate module/path is not false-rejected."""
    argv = tuple(_ACCEPTED_COMMANDS[case_id])
    # returns None (no raise) — the command parses through the grammar unchanged.
    check_declaration_argv(argv, declared_names=_DECLARED, field="start_cmd")


# ---- emission boundary (defense in depth): a credential-bearing spec cannot emit --


def _node_spec(start_cmd: tuple[str, ...]) -> ReleaseSpec:
    return ReleaseSpec(
        kind="web",
        name="R1 Node App",
        version_seq=1,
        tree_digest=_DIGEST,
        services=(
            ReleaseService(
                id="web",
                role=ServiceRole.ingress,
                runtime=RuntimeStrategy.node,
                package_manager="npm",
                lockfile="package-lock.json",
                install_cmd=("npm", "ci"),
                start_cmd=start_cmd,
                port_env="PORT",
            ),
        ),
        provenance=DetectorProvenance(
            detector="release-detect",
            detector_version="1",
            assessment=ReleaseAssessment.candidate,
            evidence=("package.json start script",),
        ),
    )


def test_emit_local_compose_refuses_credential_in_start_cmd() -> None:
    """Defense in depth: even a schema-valid ``ReleaseSpec`` (token hygiene accepts a
    shell-inert credential literal) is REFUSED at emission — ``emit_local_compose``
    raises value-free rather than lowering the credential into the Dockerfile CMD /
    release.json. This is the emission boundary's independent guard."""
    spec = _node_spec(("node", _CREDENTIALS_BY_FAMILY["npm"], "server.js"))
    with pytest.raises(ValueError) as exc:
        emit_local_compose(spec)
    assert _CREDENTIALS_BY_FAMILY["npm"] not in str(exc.value)


def test_emit_local_compose_still_emits_benign_command() -> None:
    """The emission guard does not false-reject a benign start command: a plain
    ``npm start`` spec emits its overlay, and the credential marker never appears."""
    overlay = emit_local_compose(_node_spec(("npm", "start")))
    assert overlay, "a benign spec must still emit a self-host overlay"
    blob = "\n".join(overlay.values())
    for value in _CREDENTIALS_BY_FAMILY.values():
        assert value not in blob


# ---- migrate_cmd: head-agnostic positional-credential rail (defense in depth) --

# Benign migration commands that must stay ACCEPTED — they head with a non-runtime
# migration tool (outside the head grammar) and carry only short subcommands / dotted
# module paths as operands, never a credential blob.
_BENIGN_MIGRATE_CMDS: dict[str, tuple[str, ...]] = {
    "alembic": ("alembic", "-c", "alembic.ini", "upgrade", "head"),
    "wrangler": ("wrangler", "d1", "migrations", "apply"),
    "manage_py": ("python", "manage.py", "migrate"),
    "npm_run": ("npm", "run", "migrate"),
}


@pytest.mark.parametrize("case_id", sorted(_BENIGN_MIGRATE_CMDS))
def test_check_no_positional_credential_accepts_benign_migrate(case_id: str) -> None:
    """``check_no_positional_credential`` accepts every benign migration command with
    EXACT argv — a migration tool head, short subcommands, and a dotted module path are
    never mistaken for a credential."""
    check_no_positional_credential(
        _BENIGN_MIGRATE_CMDS[case_id], declared_names=_DECLARED, field="migrate_cmd"
    )


def test_check_no_positional_credential_rejects_positional_and_role() -> None:
    """A bare positional literal credential in a migrate command — and a ``config set``
    credential-role form — is rejected value-free by the head-agnostic rail."""
    for argv in (
        ("alembic", "upgrade", _CREDENTIALS_BY_FAMILY["npm"]),  # positional shape
        ("wrangler", "d1", "execute", _CREDENTIALS_BY_FAMILY["generic_high_entropy"]),
        ("npm", "config", "set", "//registry.example/:_authToken", "plainliteralvalue123"),  # role
    ):
        with pytest.raises(ValueError) as exc:
            check_no_positional_credential(argv, declared_names=_DECLARED, field="migrate_cmd")
        for value in _CREDENTIALS_BY_FAMILY.values():
            assert value not in str(exc.value)


def test_check_no_positional_credential_allows_declared_reference_in_migrate() -> None:
    """A credential passed as a whole DECLARED ``${NAME}`` reference in a migrate command
    is accepted — the rail rejects literal VALUES, not the safe reference form."""
    check_no_positional_credential(
        ("wrangler", "d1", "execute", "${NPM_TOKEN}"),
        declared_names=_DECLARED,
        field="migrate_cmd",
    )


def _spec_with_migrate(migrate_cmd: tuple[str, ...]) -> ReleaseSpec:
    return ReleaseSpec(
        kind="web",
        name="R1 Node App",
        version_seq=1,
        tree_digest=_DIGEST,
        services=(
            ReleaseService(
                id="web",
                role=ServiceRole.ingress,
                runtime=RuntimeStrategy.node,
                package_manager="npm",
                lockfile="package-lock.json",
                install_cmd=("npm", "ci"),
                start_cmd=("npm", "start"),
                port_env="PORT",
            ),
        ),
        env=(EnvVarDecl(name="DATABASE_URL", scope=EnvScope.runtime, required=True, binding="db"),),
        resources=(
            ResourceDecl(
                id="db",
                kind=ResourceKind.sqlite,
                persistent_path="/data/app.db",
                profiles=ResourceProfiles(
                    local=LocalResourceProfile(url="file:/data/app.db", volume="app-data")
                ),
                consumers=("web",),
                migrate_cmd=migrate_cmd,
            ),
        ),
        provenance=DetectorProvenance(
            detector="release-detect",
            detector_version="1",
            assessment=ReleaseAssessment.candidate,
            evidence=("package.json start script",),
        ),
    )


def test_emit_local_compose_refuses_credential_in_resource_migrate_cmd() -> None:
    """Emission defense in depth extends to a resource ``migrate_cmd``: a bare positional
    literal credential there is REFUSED at emission, so it never ships in the compose
    migrate command / release.json."""
    spec = _spec_with_migrate(("alembic", "upgrade", _CREDENTIALS_BY_FAMILY["npm"]))
    with pytest.raises(ValueError) as exc:
        emit_local_compose(spec)
    assert _CREDENTIALS_BY_FAMILY["npm"] not in str(exc.value)


def test_emit_local_compose_emits_benign_resource_migrate_cmd() -> None:
    """A benign resource ``migrate_cmd`` still emits, with the credential markers absent
    from every overlay byte."""
    overlay = emit_local_compose(
        _spec_with_migrate(("alembic", "-c", "alembic.ini", "upgrade", "head"))
    )
    assert overlay
    blob = "\n".join(overlay.values())
    for value in _CREDENTIALS_BY_FAMILY.values():
        assert value not in blob
