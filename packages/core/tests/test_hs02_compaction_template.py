"""HS-02 — Anchored-checkpoint compaction template (binary observables).

The summarizer's instruction is the OBSERVABLE contract: a stronger-model
critic reading the built CompletionRequest should see the anchored 6-heading
template, the update-in-place vs create-fresh branching, and the A-S4
FAILED-approaches preservation. This test pins the structure (not the prose)
— the contract is what the model is told, not what it produces.

The three things the test asserts:

  (a) Create-fresh (no prior anchored summary in `messages`): the final
      user message in the built CompletionRequest carries all six anchored
      headings (GOAL / CONSTRAINTS / PROGRESS / DECISIONS / NEXT / FILES).
  (b) Update-in-place (a prior anchored summary IS in `messages`, marked
      by the stable `GOAL:` heading): the built request carries the
      UPDATE-in-place directive, and that directive is observably different
      from the create-fresh one.
  (c) Both directives demand FAILED-approaches preservation (the A-S4
      guarantee that survived into HS-02 — load-bearing: the agent must
      never lose "things tried and failed + why").

These are binary observables on the built request / the selected
instruction. They do NOT touch a real model — a `FakeModelProvider`
recorder captures the CompletionRequest the summarizer built.
"""

from __future__ import annotations

from disco.core import LLMMessage
from disco.core.llm import RouterSummarizer
from disco.core.llm.summarizer import (
    _SUMMARIZE_FRESH_INSTRUCTION,
    _SUMMARIZE_UPDATE_INSTRUCTION,
    _has_prior_anchored_summary,
    _select_summarize_instruction,
)
from llm_fakes import FakeModelProvider, build_router

# The six anchored headings the create-fresh directive mandates and the
# update-in-place directive preserves. Pinned as a module constant so a
# regression on any one of them is a single diff line.
ANCHORED_HEADINGS: tuple[str, ...] = (
    "GOAL:",
    "CONSTRAINTS:",
    "PROGRESS:",
    "DECISIONS:",
    "NEXT:",
    "FILES:",
)


def _recording_summarizer() -> tuple[RouterSummarizer, FakeModelProvider]:
    """Build a RouterSummarizer over a fake SUMMARIZER-role provider that
    records the CompletionRequest it actually receives. The summarizer
    routes the SUMMARIZER role to the `ollama` (local) provider in the
    fake config, so we replace the local provider with a recorder."""
    router, _, providers = build_router()
    recorder = FakeModelProvider("ollama", text="OK")
    providers["ollama"] = recorder
    return RouterSummarizer(router), recorder


def _final_user_message(req) -> str:
    """The summarizer always appends the directive as a final user message;
    pin the shape (role=user, last) and return the content."""
    assert req.messages, "summarizer built an empty request"
    last = req.messages[-1]
    assert last.role == "user", (
        f"final message must be the directive (user), got role={last.role!r}"
    )
    assert isinstance(last.content, str)
    return last.content


# ---- (a) Create-fresh: all six headings present, profile unchanged ---------


async def test_create_fresh_directive_has_all_six_anchored_headings():
    """First condensation (no prior summary in `messages`) → CREATE-fresh
    directive. The final user message must carry all six anchored headings
    (GOAL / CONSTRAINTS / PROGRESS / DECISIONS / NEXT / FILES) — the
    anchored template is the new contract. Profile / temperature / role
    stay byte-identical to the old summarizer call (router contract §9.1)."""
    summ, recorder = _recording_summarizer()
    messages = [LLMMessage(role="user", content="some prior history")]

    await summ.summarize(messages)

    assert recorder.seen_requests, "summarizer made no call to the provider"
    req = recorder.seen_requests[-1]
    # Profile / temperature / role are unchanged from the old summarize().
    assert req.profile.role.value == "summarizer"  # routed as SUMMARIZER role
    assert req.temperature == 0.0  # deterministic, cheap local model
    # The six anchored headings must all appear, in the directive.
    directive = _final_user_message(req)
    for heading in ANCHORED_HEADINGS:
        assert heading in directive, f"create-fresh directive missing heading: {heading!r}"
    # And the create-fresh signature: the directive opens with a condense-
    # from-above framing, NOT an update-in-place framing.
    assert "Condense the conversation above" in directive
    assert "UPDATE the prior summary IN PLACE" not in directive


# ---- (b) Update-in-place: prior summary present, directive is different ----


async def test_prior_anchored_summary_in_messages_selects_update_in_place():
    """When a prior anchored summary (marked by the `GOAL:` heading) is in
    `messages`, the summarizer MUST select the UPDATE-in-place directive.
    The two directives are observably different on the built request —
    the update one mandates the merge / restate-only-changed / preserve-
    template procedure and explicitly forbids a fresh recap."""
    summ, recorder = _recording_summarizer()
    prior = (
        "GOAL: ship the feature\n"
        "CONSTRAINTS: must run on python 3.11+\n"
        "PROGRESS: step 1 done\n"
        "DECISIONS: use uv for the build\n"
        "NEXT: implement step 2\n"
        "FILES: src/foo.py — main entry point\n"
    )
    messages = [
        LLMMessage(role="user", content="earlier context"),
        LLMMessage(role="user", content=prior),  # the prior anchored summary
        LLMMessage(role="user", content="more recent events to merge in"),
    ]

    await summ.summarize(messages)

    req = recorder.seen_requests[-1]
    directive = _final_user_message(req)
    # Update-in-place signature: must be present and different from create-
    # fresh. We assert on multiple distinct phrases to catch a regression
    # that quietly regressed the update directive to the fresh one.
    assert "UPDATE the prior summary IN PLACE" in directive
    assert "MERGE the new events" in directive
    assert "RESTATE ONLY sections that have CHANGED" in directive
    assert "PRESERVE the same template" in directive
    # And it must NOT be the create-fresh directive (different observable).
    assert "Condense the conversation above" not in directive
    # FAILED-approaches preservation must still be demanded in the update.
    assert "FAILED" in directive  # the explicit A-S4 carry-over


# ---- (c) FAILED-approaches preservation in BOTH directives -----------------


async def test_failed_approaches_preservation_in_both_directives():
    """The A-S4 guarantee carried into HS-02: FAILED approaches (tried and
    why they failed) are load-bearing — both directives must demand they
    be preserved. The create-fresh directive folds them under CONSTRAINTS
    (plus a NEXT cross-reference); the update directive demands they be
    appended/updated under CONSTRAINTS or NEXT. Either way, the word
    'FAILED' (or 'failed') must be present in the directive the model sees."""
    summ, recorder = _recording_summarizer()

    # Arm 1: create-fresh (no prior summary).
    await summ.summarize([LLMMessage(role="user", content="no prior summary")])
    fresh_directive = _final_user_message(recorder.seen_requests[-1])
    assert "FAILED" in fresh_directive or "failed" in fresh_directive, (
        "create-fresh directive lost the FAILED-approaches preservation"
    )

    # Arm 2: update-in-place (prior anchored summary present).
    recorder.seen_requests.clear()
    prior = (
        "GOAL: ship the feature\n"
        "CONSTRAINTS: must run on python 3.11+\n"
        "PROGRESS: step 1 done\n"
        "DECISIONS: use uv\n"
        "NEXT: implement step 2\n"
        "FILES: src/foo.py — main entry\n"
    )
    await summ.summarize([LLMMessage(role="user", content=prior)])
    update_directive = _final_user_message(recorder.seen_requests[-1])
    assert "FAILED" in update_directive or "failed" in update_directive, (
        "update-in-place directive lost the FAILED-approaches preservation"
    )


# ---- (d) Branching is observable, not just prose ----------------------------


async def test_create_fresh_and_update_in_place_are_observably_different():
    """Sanity: the two directives are byte-different on the built request.
    This catches a regression that breaks the marker scan (e.g. a helper
    that always returns one instruction regardless of the input)."""
    summ, recorder = _recording_summarizer()

    await summ.summarize([LLMMessage(role="user", content="no prior summary")])
    fresh = _final_user_message(recorder.seen_requests[-1])

    recorder.seen_requests.clear()
    prior = "GOAL: x\nCONSTRAINTS: y\nPROGRESS: z\nDECISIONS: w\nNEXT: v\nFILES: u\n"
    await summ.summarize([LLMMessage(role="user", content=prior)])
    update = _final_user_message(recorder.seen_requests[-1])

    assert fresh != update, "create-fresh and update-in-place must be different"
    # The two directives are module-level constants on the summarizer; pin
    # the match so a refactor that drops one is a single diff.
    assert fresh == _SUMMARIZE_FRESH_INSTRUCTION
    assert update == _SUMMARIZE_UPDATE_INSTRUCTION


# ---- (e) The marker scan is content-typed and case-sensitive ---------------


async def test_marker_scan_detects_goal_prefix_in_any_message():
    """The detection helper scans for the `GOAL:` heading prefix. A normal
    turn (e.g. user/agent chat) MUST NOT contain it; a turn that is a prior
    anchored summary MUST. This pins the marker contract on the helper."""
    # No prior summary → no detection.
    assert (
        _has_prior_anchored_summary([LLMMessage(role="user", content="just a normal user turn")])
        is False
    )
    # Empty messages list → no detection.
    assert _has_prior_anchored_summary([]) is False
    # The prior summary, anywhere in the message list, fires the detection —
    # the scan walks every message, not just the last.
    assert (
        _has_prior_anchored_summary(
            [
                LLMMessage(role="user", content="earlier context"),
                LLMMessage(role="user", content="GOAL: ship the feature\n..."),
            ]
        )
        is True
    )
    # A lowercase "goal:" is NOT the marker (the directive mandates the
    # uppercase prefix); this guards against a too-loose scan.
    assert (
        _has_prior_anchored_summary(
            [LLMMessage(role="user", content="goal: lowercase is not the marker")]
        )
        is False
    )
    # An empty-string content is also a non-match (the marker is the
    # `GOAL:` prefix, not the empty string).
    assert _has_prior_anchored_summary([LLMMessage(role="user", content="")]) is False
    # The defensive `isinstance(c, str)` check in the helper means a non-
    # string `content` (if a future schema ever loosens the type) is
    # silently skipped, not crashed on. We can't construct that through
    # LLMMessage today, so this is a contract pin on the helper, not a
    # data-driven assertion.


async def test_marker_scan_uses_goal_prefix_not_other_anchored_headings():
    """The marker is specifically the LEADING heading (`GOAL:`) — not just
    any anchored heading. A message that contains CONSTRAINTS / FILES /
    etc. but no GOAL must NOT trigger the update directive (otherwise a
    user who pastes a plan-like list mid-conversation would force
    update-in-place on the next condensation)."""
    partial = (
        "CONSTRAINTS: must run on python 3.11+\n"
        "PROGRESS: step 1 done\n"
        "DECISIONS: use uv\n"
        "NEXT: implement step 2\n"
        "FILES: src/foo.py\n"
    )
    assert _has_prior_anchored_summary([LLMMessage(role="user", content=partial)]) is False
    # But the full template, leading with GOAL:, does.
    full = "GOAL: ship it\n" + partial
    assert _has_prior_anchored_summary([LLMMessage(role="user", content=full)]) is True


# ---- (f) The directive selector mirrors the marker scan --------------------


def test_select_summarize_instruction_observable_branching():
    """The selector returns the create-fresh constant when no prior
    summary is present, and the update-in-place constant when one is.
    A reviewer can read this test and see both arms of the branch without
    driving a full summarize() call."""
    no_prior: list[LLMMessage] = [LLMMessage(role="user", content="no prior")]
    assert _select_summarize_instruction(no_prior) == _SUMMARIZE_FRESH_INSTRUCTION
    # With the prior anchored summary in the list, the update directive
    # wins. The selector does not care which message holds the marker.
    with_prior_middle: list[LLMMessage] = [
        LLMMessage(role="user", content="earlier context"),
        LLMMessage(role="user", content="GOAL: ship it\n..."),
        LLMMessage(role="user", content="more recent context"),
    ]
    assert _select_summarize_instruction(with_prior_middle) == _SUMMARIZE_UPDATE_INSTRUCTION
