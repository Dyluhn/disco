"""A model PICK drives the WHOLE generative pipeline — not just the brain.

The bug: picking an OpenRouter model (e.g. DeepSeek) only reassigned AGENT_DRIVER,
so the summarizer / rewriter / answerer kept running on local models. Picking a
model now means that model does the entire generative job; only the NLI verifier
(a cross-encoder, not a chat model) stays on its own assignment.
"""

from __future__ import annotations

from disco.agent_server import ConversationRuntime
from disco.core import SqliteEventStore
from disco.core.llm import ConfigStore, SecretBox, SecretStore
from disco.core.llm.types import ModelRole

_GENERATIVE = (
    ModelRole.AGENT_DRIVER,
    ModelRole.RAG_ANSWERER,
    ModelRole.QUERY_REWRITER,
    ModelRole.SUMMARIZER,
)


def _runtime(tmp_path) -> ConversationRuntime:
    return ConversationRuntime(
        SqliteEventStore(":memory:"),
        config_store=ConfigStore(tmp_path / "config.json"),
        secret_store=SecretStore(tmp_path / "secrets.json", box=SecretBox(None)),
    )


def _other_model(cfg, avoid) -> str:
    # any catalogue key that isn't already the driver assignment
    for k in cfg.models:
        if k != avoid:
            return k
    raise AssertionError("need >=2 models in the default catalogue")


def test_pick_reassigns_every_generative_role(tmp_path):
    rt = _runtime(tmp_path)
    base = rt._config_store.load()
    pick = _other_model(base, base.assignments.get(ModelRole.AGENT_DRIVER))

    router = rt._router_now(pick=pick)
    asg = router._config.assignments
    # Every generative role now resolves to the picked model.
    for role in _GENERATIVE:
        assert asg[role] == pick, f"{role} should follow the pick"
    # The NLI verifier (cross-encoder) is NOT a chat model → left on its assignment.
    assert asg.get(ModelRole.NLI_VERIFIER) == base.assignments.get(ModelRole.NLI_VERIFIER)


def test_no_pick_leaves_assignments_untouched(tmp_path):
    rt = _runtime(tmp_path)
    base = rt._config_store.load()
    router = rt._router_now(pick=None)
    assert router._config.assignments == base.assignments


def test_unknown_pick_fails_safe_to_saved_assignments(tmp_path):
    rt = _runtime(tmp_path)
    base = rt._config_store.load()
    router = rt._router_now(pick="no-such-model-key")
    # An unknown key is ignored — we don't blow up or strand a role on a missing model.
    assert router._config.assignments == base.assignments
