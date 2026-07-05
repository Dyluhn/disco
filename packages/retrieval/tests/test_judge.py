"""A4.0 — LLM-judge claim gate tests (fake router, no network)."""

from __future__ import annotations

import pytest

from disco.retrieval.deep_research.judge import (
    ClaimVerdict,
    fraction_supported,
    judge_claim,
    judge_claims,
    parse_verdict,
    weak_claims,
)


class _Resp:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeRouter:
    """Returns scripted replies in order; records the prompts it saw."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.prompts: list[str] = []

    async def complete(self, req, *, context=None):  # noqa: ANN001
        self.prompts.append(req.messages[-1].content)
        return _Resp(self._replies.pop(0) if self._replies else "UNSUPPORTED")


# ---- parse_verdict: the UNSUPPORTED-contains-SUPPORTED trap ------------------


def test_parse_unsupported_not_misread_as_supported() -> None:
    # "UNSUPPORTED" contains the substring "SUPPORTED" — must NOT map to SUPPORTED.
    assert parse_verdict("UNSUPPORTED") == "UNSUPPORTED"
    assert parse_verdict("unsupported.") == "UNSUPPORTED"
    assert parse_verdict("Verdict: UNSUPPORTED") == "UNSUPPORTED"


def test_parse_supported_partial() -> None:
    assert parse_verdict("SUPPORTED") == "SUPPORTED"
    assert parse_verdict("  supported  ") == "SUPPORTED"
    assert parse_verdict("PARTIAL") == "PARTIAL"
    assert parse_verdict("partial — weak") == "PARTIAL"


def test_parse_unknown_defaults_conservative() -> None:
    # Unparseable → UNSUPPORTED so a parse failure never FALSELY stops the loop.
    assert parse_verdict("") == "UNSUPPORTED"
    assert parse_verdict("I'm not sure") == "UNSUPPORTED"


# ---- judge_claim / judge_claims ---------------------------------------------


@pytest.mark.asyncio
async def test_judge_claim_calls_router_and_returns_verdict() -> None:
    router = _FakeRouter(["SUPPORTED"])
    v = await judge_claim("The sky is blue.", ["Rayleigh scattering makes the sky blue."], router=router)
    assert v == ClaimVerdict(claim="The sky is blue.", verdict="SUPPORTED")
    # The prompt carried the claim + the cited passage.
    assert "The sky is blue." in router.prompts[0]
    assert "Rayleigh scattering" in router.prompts[0]


@pytest.mark.asyncio
async def test_judge_claims_each_claim() -> None:
    router = _FakeRouter(["SUPPORTED", "PARTIAL", "UNSUPPORTED"])
    verdicts = await judge_claims(
        [("a", ["p"]), ("b", ["q"]), ("c", ["r"])],
        router=router,
    )
    assert [v.verdict for v in verdicts] == ["SUPPORTED", "PARTIAL", "UNSUPPORTED"]


# ---- the convergence gate helpers -------------------------------------------


def test_fraction_supported_and_weak_claims() -> None:
    vs = [
        ClaimVerdict("a", "SUPPORTED"),
        ClaimVerdict("b", "SUPPORTED"),
        ClaimVerdict("c", "PARTIAL"),
        ClaimVerdict("d", "UNSUPPORTED"),
    ]
    assert fraction_supported(vs) == 0.5
    assert [v.claim for v in weak_claims(vs)] == ["c", "d"]


def test_fraction_supported_empty_is_vacuously_one() -> None:
    # No claims → nothing to re-search → the gate is satisfied (1.0), not a div-by-zero.
    assert fraction_supported([]) == 1.0
    assert weak_claims([]) == []


async def test_think_only_judge_retries_with_wider_budget():
    """BUDGET TRAP: a reasoning judge spends max_tokens=8 inside <think> — the
    stripped-empty first reply must trigger ONE widened retry, not a blanket
    UNSUPPORTED that turns the refinement loop maximally harsh."""
    calls: list[int | None] = []

    class _Router:
        async def complete(self, req, *, context=None):  # noqa: ANN001
            calls.append(req.max_tokens)
            text = "<think>the claim seems" if len(calls) == 1 else "<think>ok</think>SUPPORTED"
            return type("R", (), {"text": text})()

    v = await judge_claim("a claim", ["a passage"], router=_Router())
    assert v.verdict == "SUPPORTED"
    assert calls == [8, 512]


def test_parse_verdict_never_matches_inside_think():
    assert parse_verdict("<think>this is not UNSUPPORTED because…</think>SUPPORTED") == "SUPPORTED"
