"""B2 acceptance: bundled-llm launch command emits ctx=8192 and KV quant q8_0.

The bundled llama-server is the `llm` service in compose.yaml (profiles:
["bundled-llm"]). This test parses the static YAML and asserts that the
rendered command defaults contain:
  -c 8192      (via ${PMX_LLM_CTX:-8192})
  -ctk q8_0   (via ${PMX_LLM_CTK:-q8_0})
  -ctv q8_0   (via ${PMX_LLM_CTV:-q8_0})

It also asserts that no OTHER compose service carries -ctk/-ctv, confirming
the KV-quant flags are bundled-only.  Seed_config.py's PMX_DRIVER_CTX
default is checked for consistency.

Run: uv run pytest harness/tests/test_bundled_launch_args.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
COMPOSE_PATH = REPO_ROOT / "compose.yaml"
SEED_PATH = REPO_ROOT / "scripts" / "seed_config.py"


def _load_compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text())


def _cmd_str(svc: dict) -> str:
    cmd = svc.get("command", "")
    if isinstance(cmd, list):
        return " ".join(str(t) for t in cmd)
    return str(cmd)


# ---------------------------------------------------------------------------
# Structural: confirm we're actually looking at the bundled profile
# ---------------------------------------------------------------------------


def test_llm_service_exists():
    compose = _load_compose()
    assert "llm" in compose["services"], "compose.yaml must have an `llm` service"


def test_llm_service_in_bundled_llm_profile():
    compose = _load_compose()
    svc = compose["services"]["llm"]
    assert "bundled-llm" in svc.get("profiles", []), (
        "llm service must declare profiles: [bundled-llm] to be the BUNDLED path"
    )


# ---------------------------------------------------------------------------
# Core B2 assertions: ctx=8192, -ctk q8_0, -ctv q8_0
# ---------------------------------------------------------------------------


def test_bundled_ctx_default_is_8192():
    compose = _load_compose()
    cmd = _cmd_str(compose["services"]["llm"])
    # Accept ${PMX_LLM_CTX:-8192} (env-var form) or bare -c 8192
    assert re.search(r"-c\s+(?:\$\{PMX_LLM_CTX:-)?8192\}?", cmd), (
        f"Bundled llm command must have -c 8192 (or ${{PMX_LLM_CTX:-8192}}). Got:\n{cmd!r}"
    )


def test_bundled_ctk_default_is_q8_0():
    compose = _load_compose()
    cmd = _cmd_str(compose["services"]["llm"])
    assert re.search(r"-ctk\s+(?:\$\{PMX_LLM_CTK:-)?q8_0\}?", cmd), (
        f"Bundled llm command must have -ctk q8_0 (or ${{PMX_LLM_CTK:-q8_0}}). Got:\n{cmd!r}"
    )


def test_bundled_ctv_default_is_q8_0():
    compose = _load_compose()
    cmd = _cmd_str(compose["services"]["llm"])
    assert re.search(r"-ctv\s+(?:\$\{PMX_LLM_CTV:-)?q8_0\}?", cmd), (
        f"Bundled llm command must have -ctv q8_0 (or ${{PMX_LLM_CTV:-q8_0}}). Got:\n{cmd!r}"
    )


# ---------------------------------------------------------------------------
# Isolation: other services must NOT carry -ctk/-ctv
# ---------------------------------------------------------------------------


def test_non_bundled_services_have_no_kv_quant_flags():
    compose = _load_compose()
    for name, svc in compose["services"].items():
        if name == "llm":
            continue  # the bundled service itself is the intended bearer
        cmd = _cmd_str(svc)
        assert "-ctk" not in cmd, (
            f"Service {name!r} must not carry -ctk (KV quant is bundled-only). cmd={cmd!r}"
        )
        assert "-ctv" not in cmd, (
            f"Service {name!r} must not carry -ctv (KV quant is bundled-only). cmd={cmd!r}"
        )


# ---------------------------------------------------------------------------
# Consistency: seed_config.py PMX_DRIVER_CTX default matches server default
# ---------------------------------------------------------------------------


def test_seed_config_driver_ctx_default_is_8192():
    """seed_config.py must default PMX_DRIVER_CTX to 8192 (matches server default)."""
    src = SEED_PATH.read_text()
    assert 'os.environ.get("PMX_DRIVER_CTX", "8192")' in src, (
        "seed_config.py PMX_DRIVER_CTX default must be 8192 to match the bundled server ctx.\n"
        f"Searched in: {SEED_PATH}"
    )


# ---------------------------------------------------------------------------
# B1b keyless wiring: the bundled .env.example selects the lite encoder tier so a
# fresh 8 GB keyless install fits without the operator knowing to opt in. (Code
# defaults to full to avoid regressing existing deployments; the bundled env is
# where the keyless default is actually wired to lite.)
# ---------------------------------------------------------------------------


def test_bundled_env_example_selects_lite_encoder_tier():
    """.env.example (the bundled/keyless default env) must set PMX_ENCODER_TIER=lite."""
    env = (REPO_ROOT / ".env.example").read_text()
    assert re.search(r"^PMX_ENCODER_TIER=lite\s*$", env, re.MULTILINE), (
        "the bundled .env.example must default PMX_ENCODER_TIER=lite so the keyless "
        "8 GB install gets the small (~0.15 GB) encoders out of the box."
    )
