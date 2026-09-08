"""Short ordinal citation aliases — the ids the report writer actually cites.

A canonical passage id is an opaque handle (``3ef99c_p1``). That is the right
identity for the claim ledger, the ``ReportEvent``, and the frontend, and it is
the WRONG identity to put in front of a writing model: with sixty of them in one
context a local writer stops copying them and falls back to its natural indexing
instinct. A live run did exactly that — the draft cited ``[[id1]]`` through
``[[id60]]``, ids of the model's own invention, and it produced the same sixty
again through both reworks while being told, by name, that every one of them
resolved to nothing. Unresolvable citations are structural, so the run errored
and the whole research was lost.

Feedback cannot fix an instinct. So the instinct is made CORRECT instead: the
evidence pool is labelled ``s1``…``sN`` in pool order, the prompt's citation
contract is stated in those labels, and index-style citation becomes the valid
format. Every citation the writer produces is translated back to canonical ids
HERE, at the boundary, before any deterministic check, the claim ledger, or the
event boundary sees it. Nothing downstream changes: past this translation the
whole system still speaks canonical passage ids.

The tolerance is deliberately narrow — one alias format, taught by the prompt:

* ``[[s12]]``, ``[[S12]]``, and the bare ``[s12]`` all resolve (the bare form
  through the same ``normalize_citations`` promotion that already handles bare
  canonical ids).
* An alias OUT of range (``[[s99]]`` against a sixty-passage pool) is NOT
  translated. It flows through as an unresolvable citation exactly as before,
  because a source that does not exist is a fabrication and must stay caught.
* ``id12``, ``ref12``, and every other invented scheme are NOT accepted. The
  unresolvable-citation finding names the valid range instead, so a
  scheme-inventing model meets an exact wall at an exact angle.
* A canonical passage id the writer cites directly is left exactly as it is.
  The truth is never punished for being the truth, and it also settles the
  degenerate case where a pool id happens to look like an alias.

Translation runs both ways. Canonical is the STORED form — review findings,
the claim ledger's identities, the unverified sentences, the shipped report —
and the alias view is rendered only into the bytes a model reads.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from ..models import Passage
from ._writer_parts import normalize_citations
from ._writer_prompts import CITATION_ID_CONTRACT, VALID_CITATION_IDS

# `s` for "source". One letter plus an ordinal is the shortest identity a model
# can copy without transcription error, and it is far enough from `id`/`ref`
# that the prompt's contract and the finding's range read unambiguously.
ALIAS_PREFIX = "s"

# The same marker shape `_writer_parts._CITATION` parses, so translation and
# citation extraction can never disagree about what a citation IS.
_CITATION_MARKER = re.compile(r"\[\[([\w-]+)\]\]")


@dataclass(frozen=True)
class CitationAliases:
    """One run's two-way map between ordinal aliases and canonical passage ids.

    Built once per report run from the pool in the order the evidence block is
    formatted in, so ``s3`` is always the third source the writer was shown.
    An empty map (no passages) makes every method the identity function, which
    is what the writer's own "no usable evidence" error path needs.
    """

    alias_by_passage: Mapping[str, str] = field(default_factory=dict[str, str])
    passage_by_alias: Mapping[str, str] = field(default_factory=dict[str, str])

    @property
    def range_hint(self) -> str:
        """``s1-s60`` — the exact valid range, for the finding and prompt.

        Empty when the run has no aliases, so a caller can drop the whole
        sentence rather than print a nonsense range.
        """
        if not self.alias_by_passage:
            return ""
        aliases = list(self.alias_by_passage.values())
        first, last = aliases[0], aliases[-1]
        return first if first == last else f"{first}-{last}"

    @property
    def promotable_ids(self) -> set[str]:
        """Alias spellings a bare ``[s12]`` may be promoted from.

        Both cases, because the doubled form tolerates both and a bare citation
        is the same citation with one bracket missing.
        """
        return {
            spelling
            for alias in self.alias_by_passage.values()
            for spelling in (alias, alias.upper())
        }

    def prompt_contract(self) -> str:
        """The report prompt's statement of the closed set of valid ids.

        Empty when the run assigns no aliases, so an alias-less caller prints
        no contract rather than a contract naming nothing.
        """
        ids = self.range_hint
        return CITATION_ID_CONTRACT.format(ids=ids) if ids else ""

    def valid_ids_line(self) -> str:
        """The sentence appended to the unresolvable-citation finding: the
        wall the model already hit, now with the angle it needs."""
        ids = self.range_hint
        return VALID_CITATION_IDS.format(ids=ids) if ids else ""

    def to_canonical(self, text: str) -> str:
        """Model-written text with its alias citations resolved to passage ids.

        Runs on every span of writer output before anything grades or stores it.
        Unknown tokens — an out-of-range alias, an invented scheme, a canonical
        id — are returned untouched, so this can only ever resolve a citation,
        never hide a broken one.
        """
        if not self.passage_by_alias:
            return text
        promoted = normalize_citations(text, self.promotable_ids)
        return _CITATION_MARKER.sub(self._resolve, promoted)

    def to_alias(self, text: str) -> str:
        """The same text rendered in the ids the model was taught.

        Used for the bytes a model reads back — a rendered finding, the
        reviewer's view of the draft — so the writer never has to hold two
        identities for one source at once.
        """
        if not self.alias_by_passage:
            return text
        return _CITATION_MARKER.sub(self._label_marker, text)

    def _resolve(self, match: re.Match[str]) -> str:
        token = match.group(1)
        # A canonical pool id is already the truth; leave it exactly as written.
        if token in self.alias_by_passage:
            return match.group(0)
        passage_id = self.passage_by_alias.get(token.casefold())
        return match.group(0) if passage_id is None else f"[[{passage_id}]]"

    def _label_marker(self, match: re.Match[str]) -> str:
        return f"[[{self.alias_by_passage.get(match.group(1), match.group(1))}]]"


def citation_aliases(passages: Sequence[Passage]) -> CitationAliases:
    """Assign ``s1``…``sN`` over the pool, in the order the writer is shown it."""
    alias_by_passage: dict[str, str] = {}
    passage_by_alias: dict[str, str] = {}
    for index, passage in enumerate(passages, start=1):
        alias = f"{ALIAS_PREFIX}{index}"
        alias_by_passage[passage.id] = alias
        passage_by_alias[alias] = passage.id
    return CitationAliases(alias_by_passage=alias_by_passage, passage_by_alias=passage_by_alias)


# The identity translation. A caller with no aliases assigned — a helper driven
# directly, a run whose pool never reached the writer — passes this and gets
# exactly today's behaviour: canonical ids in, canonical ids out, no contract
# sentence and no range.
NO_ALIASES = CitationAliases()


__all__ = ["ALIAS_PREFIX", "NO_ALIASES", "CitationAliases", "citation_aliases"]
