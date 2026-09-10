"""Finite output capacity and recovery for deep-research model calls.

An output ceiling bounds each provider request, including reasoning and visible
text. Reaching it may mean legitimate reasoning needed more capacity or that
generation failed to converge; the finish reason alone cannot distinguish them.
Incomplete decisions are never applied. The existing finite turn allowance
permits a replacement request with a larger ceiling, up to the shared hard cap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from disco.core.env import disco_env
from disco.core.llm import CompletionResponse

# Initial capacity includes reasoning and the visible research decision.
_DEFAULT_RESEARCH_TURN_MAX_TOKENS = 24_000

# The precise re-ask. It names the actual failure (the response ran past the
# ceiling before it finished the JSON) rather than the parse symptom, so the
# model is told what to do differently instead of being told a bare no.
OUTPUT_CEILING_REASK = (
    "your previous response exceeded the output ceiling without completing the "
    "JSON; answer ONLY with the JSON turn format"
)


def research_turn_max_tokens() -> int:
    """Output ceiling for one deep-research model call, in tokens.

    Env: ``DISCO_RESEARCH_TURN_MAX_TOKENS`` (the legacy ``PMX_`` name is
    honored by ``disco_env``). An unparseable or non-positive value falls back
    to the default rather than failing a run — or, worse, un-capping one —
    over a typo'd knob. Read per call: the value is cheap, and tests and
    operators both set the env at runtime.
    """
    raw = disco_env("RESEARCH_TURN_MAX_TOKENS")
    if raw is None:
        return _DEFAULT_RESEARCH_TURN_MAX_TOKENS
    try:
        value = int(raw.strip())
    except ValueError:
        return _DEFAULT_RESEARCH_TURN_MAX_TOKENS
    return value if value > 0 else _DEFAULT_RESEARCH_TURN_MAX_TOKENS


def ceiling_hit(response: CompletionResponse, *, cap: int) -> int | None:
    """``cap`` when this response was cut off BY the ceiling, else ``None``.

    ``finish_reason == "length"`` is authoritative here even when the truncated
    text happens to parse: the JSON extractor scans for the outermost braces, so
    a loop that repeats JSON-shaped text can yield a "parseable" turn out of a
    generation that never terminated. A length finish is an incomplete provider
    response, even when a prefix happens to form a JSON object.
    """
    return cap if response.finish_reason == "length" else None


def output_ceiling_trail_row(turn: int, *, tokens: int) -> dict[str, Any]:
    """The audit row for one turn that hit the wall, so the harness can count
    runaway events instead of inferring them from a malformed-turn spike."""
    return {
        "kind": "output_ceiling",
        "stage": "research_turn",
        "turn": turn,
        "tokens": tokens,
    }


# ---------------------------------------------------------------------------
# The other half of the ceiling: what to do when a call produced no turn at
# all. Measured in the torture artifacts (round3-B/run-02, round9-B/run-01
# twice, round6-B/run-05): a research call came back HTTP 200 with
# ``finish_reason="length"`` and ZERO characters — the reasoning phase spent
# the entire allowance before the JSON began — and the ONE re-ask went out with
# the identical ceiling, carrying a message that told the model its previous
# response "exceeded the output ceiling without completing the JSON". There was
# no previous response. The re-ask re-ran a budget already proven too small and
# described a document that never existed.
#
# The RULE: grow only when the evidence says room ran out, keep the whole
# previous ceiling, add one turn provision, hard cap.
# ---------------------------------------------------------------------------

#: One research turn's JSON decision object is a few hundred tokens. This is
#: the room a REPLACEMENT call adds on top of the ceiling the reasoning phase
#: already demonstrated it wanted, so the turn gets space the thinking has not
#: already claimed.
_TURN_PROVISION_TOKENS = 4_000

#: A research turn is never provisioned past this, however many times its
#: ceiling grows. Hard, named once, denominated in work, and identical for
#: every model — a reasoning model gets the same allowance a terse one does.
#: Twice the base ceiling: a run gets at most six escalations (three malformed
#: turns, two calls each) before the malformed cap ends it anyway.
RESEARCH_CEILING_CAP = 48_000

#: The re-ask for a reply that never arrived. Its counterpart for a reply that
#: arrived and could not be used is the parse error itself; sending a parse
#: error about an empty response is how the old path told a model its silence
#: was a JSON syntax error.
EMPTY_TURN_REASK = (
    "your previous reply arrived empty — the whole output allowance went into "
    "the thinking phase before any JSON was produced. This call has a larger "
    "allowance: emit the JSON turn object first and keep the reasoning short"
)

#: The four shapes one research call can fail in. Named because the repairs
#: differ: two are the host's ceiling and two are the reply's shape.
TurnFailureShape = Literal["ceiling_hit", "empty_response", "truncated_turn", "malformed_turn"]

#: Shapes that mean nothing arrived at all. They get the empty re-ask and a
#: bigger allowance; they never get a parse error, because there is no document
#: to have a parse error about.
EMPTY_SHAPES = frozenset({"ceiling_hit", "empty_response"})


def empty_turn_error(*, finish_reason: str, ceiling: int) -> str:
    """What the audit records for a call that produced nothing.

    Says what actually happened — zero characters — instead of describing a
    JSON document that was never written.
    """
    return (
        "no turn arrived: the reply carried zero characters "
        f"(finish_reason={finish_reason}, output ceiling {ceiling} tokens)"
    )


def turn_failure_shape(*, finish_reason: str, content_chars: int) -> TurnFailureShape:
    """Which of the four shapes a failed research call was.

    Content first, provider self-report second: a reply with bytes in it was
    not starved of room whatever the finish reason says, and a reply with no
    bytes produced nothing usable whatever the finish reason says. (Hosted
    providers disagree about which finish reason a reasoning phase that ate the
    allowance reports, so the finish reason is never the discriminator.)
    """
    if content_chars == 0:
        return "ceiling_hit" if finish_reason == "length" else "empty_response"
    return "truncated_turn" if finish_reason == "length" else "malformed_turn"


@dataclass
class TurnCeiling:
    """This run's research-turn output ceiling, and how it grows.

    Run-scoped, not call-scoped, and monotone. A ceiling that reset between
    turns would re-create the measured defect one turn later: the model has
    already demonstrated it needs the room, and re-issuing into the proven-small
    budget is how a run burns its whole malformed-turn allowance on the same
    wall three times.
    """

    tokens: int

    @classmethod
    def start(cls) -> TurnCeiling:
        return cls(tokens=research_turn_max_tokens())

    def grow_after(self, *, finish_reason: str, content_chars: int, output_tokens: int) -> None:
        """Raise the ceiling when the evidence says room is what ran out.

        A reply that arrived complete and was merely unusable keeps the same
        ceiling: more room is not what it was short of, and a bigger allowance
        would only buy more reasoning.
        """
        if finish_reason != "length" and content_chars > 0:
            return
        spent = max(self.tokens, output_tokens)
        self.tokens = min(RESEARCH_CEILING_CAP, spent + _TURN_PROVISION_TOKENS)
