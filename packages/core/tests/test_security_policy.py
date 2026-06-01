"""ConfirmationPolicy + two-signal independence — contract §9.4 / §9.5."""

from __future__ import annotations

from perpleximanus.core import ActionEvent, SecurityRisk, ToolCall
from perpleximanus.core.loop import AlwaysConfirm, ConfirmRisky, NeverConfirm
from perpleximanus.core.security import RuleBasedAnalyzer

U, L, M, H = (
    SecurityRisk.UNKNOWN,
    SecurityRisk.LOW,
    SecurityRisk.MEDIUM,
    SecurityRisk.HIGH,
)
ALL = [U, L, M, H]


# ---- §9.4 the policies ------------------------------------------------------


def test_never_confirm_never_gates():
    p = NeverConfirm()
    assert all(p.should_confirm(r) is False for r in ALL)


def test_always_confirm_always_gates():
    p = AlwaysConfirm()
    assert all(p.should_confirm(r) is True for r in ALL)


def test_confirm_risky_high_default():
    p = ConfirmRisky(H, confirm_unknown=True)
    assert p.should_confirm(H) is True
    assert p.should_confirm(U) is True  # confirm-on-UNKNOWN (principle 4)
    assert p.should_confirm(M) is False
    assert p.should_confirm(L) is False


def test_confirm_risky_medium_threshold():
    p = ConfirmRisky(M)
    assert p.should_confirm(M) is True
    assert p.should_confirm(H) is True
    assert p.should_confirm(L) is False


def test_confirm_unknown_can_be_opted_out_only_deliberately():
    # The safe default gates on UNKNOWN; opting out must be explicit.
    assert ConfirmRisky(H).should_confirm(U) is True
    assert ConfirmRisky(H, confirm_unknown=False).should_confirm(U) is False


# ---- §9.5 two-signal independence -------------------------------------------


def test_self_assessment_cannot_wave_an_action_through():
    # The agent self-rates a destructive command LOW; the independent analyzer
    # scores it HIGH. The analyzer's score is authoritative — the gate fires.
    action = ActionEvent(
        thought="trust me",
        tool_call=ToolCall(tool_name="shell", arguments={"command": "rm -rf /"}),
        self_assessed_risk=L,  # the model under-rates its own action
    )
    analyzer_risk = RuleBasedAnalyzer().assess(action)
    assert analyzer_risk == H  # the analyzer is not fooled by the self-assessment
    assert ConfirmRisky(H).should_confirm(analyzer_risk) is True
