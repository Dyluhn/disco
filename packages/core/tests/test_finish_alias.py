"""P6-FINALIZERS tests: the contract verification finalizer as a per-kind alias of the
`finish` virtual tool — recognized + advertised across every parallel surface."""

from __future__ import annotations

from types import SimpleNamespace

from disco.core.contract import BuildContractRegistry, ContractKind
from disco.core.llm.types import OperatingMode, ToolSpec
from disco.core.loop.driver import Driver
from disco.core.loop.engine import is_finish_tool_name

_ALIAS = "ready_for_app_verification"


# --- the central helper -------------------------------------------------------
def test_plain_finish_always_recognized() -> None:
    assert is_finish_tool_name("finish", None) is True
    assert is_finish_tool_name("finish", _ALIAS) is True


def test_alias_recognized_only_with_contract() -> None:
    assert is_finish_tool_name(_ALIAS, _ALIAS) is True
    assert is_finish_tool_name(_ALIAS, None) is False  # no contract → alias is NOT finish


def test_other_tools_not_finish() -> None:
    assert is_finish_tool_name("file_write", _ALIAS) is False
    assert is_finish_tool_name("verify_web_app", _ALIAS) is False


def test_every_builtin_finalizer_is_recognized_when_active() -> None:
    # coherence: every contract's finalizer is the finish signal when it governs the run,
    # and is NOT recognized without it (proving the alias is required — the false
    # affordance the packs created is now closed).
    reg = BuildContractRegistry.default()
    for kind in ContractKind:
        c = reg.get(kind)
        assert c is not None
        fin = c.verify.finalizer
        assert is_finish_tool_name(fin, fin) is True, fin
        assert is_finish_tool_name(fin, None) is False, fin


# --- driver surfaces (advertisement + requery known-names + planning) ---------
def _fake_loop(*, finish_alias, mode=OperatingMode.LONG_HORIZON):
    executor = SimpleNamespace(
        available_tools=lambda: [ToolSpec(name="file_write", description="", parameters_schema={})],
        callable_tool_names=lambda: frozenset({"file_write"}),
    )
    return SimpleNamespace(
        executor=executor,
        mode=mode,
        _planning_tools=frozenset(),
        _autonomous=False,
        _finish_alias=finish_alias,
        _plan_tool="submit_plan",
    )


def _offered_names(finish_alias, mode=OperatingMode.LONG_HORIZON):
    tools = Driver(_fake_loop(finish_alias=finish_alias, mode=mode)).tools_for_step()
    return {getattr(t, "name", None) for t in tools}


def test_active_contract_advertises_the_alias() -> None:
    names = _offered_names(_ALIAS)
    assert _ALIAS in names  # the finalizer the pack tells the model to call is offered
    assert "finish" in names  # plain finish kept for compatibility


def test_no_contract_advertises_finish_only() -> None:
    names = _offered_names(None)
    assert "finish" in names
    assert _ALIAS not in names  # no fabricated finalizer for a plain build


def test_alias_not_advertised_in_planning_mode_like_finish() -> None:
    # PLANNING offers only planning tools — neither finish nor the alias (both gated same)
    names = _offered_names(_ALIAS, mode=OperatingMode.PLANNING)
    assert "finish" not in names and _ALIAS not in names


def test_alias_in_requery_known_names() -> None:
    known = Driver(_fake_loop(finish_alias=_ALIAS)).known_tool_names_for_requery()
    assert _ALIAS in known and "finish" in known
    # without a contract, the alias is not a known name
    known_plain = Driver(_fake_loop(finish_alias=None)).known_tool_names_for_requery()
    assert _ALIAS not in known_plain and "finish" in known_plain



# --- agent batched-call selection (P6 bugs 1+2) -------------------------------
def _calls(*names):
    return [SimpleNamespace(tool_name=n, arguments={"k": n}) for n in names]


def test_batched_alias_plus_action_keeps_the_real_action() -> None:
    from disco.core.loop.agent import _pick_tool_call
    # [finalizer-alias, shell] → the REAL action survives (not discarded), W-32 alias-aware
    name, args = _pick_tool_call(_calls(_ALIAS, "shell"), _ALIAS)
    assert name == "shell" and args == {"k": "shell"}
    # same for a batched plain finish + action
    assert _pick_tool_call(_calls("finish", "file_write"), _ALIAS)[0] == "file_write"


def test_alias_alone_canonicalizes_to_finish() -> None:
    from disco.core.loop.agent import _pick_tool_call
    # a lone finalizer alias → canonical "finish" (the alias name never leaks downstream)
    assert _pick_tool_call(_calls(_ALIAS), _ALIAS)[0] == "finish"
    assert _pick_tool_call(_calls("finish"), _ALIAS)[0] == "finish"
    # without a contract, the alias is just a normal tool (not finish)
    assert _pick_tool_call(_calls(_ALIAS), None)[0] == _ALIAS
