"""Every scenario must ASK for what it asserts.

A scenario that requires a file it never named grades correct work as failure:
the model picks an equally ordinary name and the run dies FALSE_FINISH_NO_OUTPUT
on a requirement it was never given. That is the campaign's own rule turned
inside out — "do not solve failures by making the model's job harder, adding
framework/filename special cases" applies to the acceptance matrix too.

`p4_ff_node_restart` asserted `server.js` while its prompt asked only for "a Node
service" (seed 406438: the model wrote index.js). Both sibling node scenarios
already named the file; this one was the outlier.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_SCENARIOS = Path(__file__).resolve().parents[1] / "scenarios_phase4.yaml"

# Entrypoints a target's own toolchain SCAFFOLDS, so the model does not choose
# them and the prompt need not dictate them: `npm create vite` writes index.html,
# and an imported source brings its own files. Anything outside this set is a free
# choice and must be asked for explicitly.
_CONVENTIONAL_ENTRYPOINTS = {"index.html", "package.json"}


def _scenarios() -> list[dict]:
    doc = yaml.safe_load(_SCENARIOS.read_text())
    raw = doc.get("scenarios", doc) if isinstance(doc, dict) else doc
    return [s for s in raw if isinstance(s, dict)]


def _fixture_supplied(scenario: dict) -> set[str]:
    """Files the scenario itself PROVIDES (an import fixture) are not a choice the
    model makes, so the prompt need not name them."""
    fixture = scenario.get("import_fixture") or {}
    files = fixture.get("files") or {}
    return set(files) if isinstance(files, dict) else set()


def _requested_text(scenario: dict) -> str:
    parts = [str(scenario.get("prompt", ""))]
    parts += [str(f.get("text", "")) for f in scenario.get("followups") or []]
    return " ".join(" ".join(p.split()) for p in parts)


@pytest.mark.parametrize("scenario", _scenarios(), ids=lambda s: str(s.get("id")))
def test_every_asserted_literal_is_requested_in_the_prompt(scenario: dict) -> None:
    """A `must_contain` literal must be something the run was actually ASKED for.

    `p4_ff_node_basic` asked for "the runtime PORT environment variable" — a
    property — and asserted the spelling `process.env.PORT`. A build writing the
    idiomatic `const { PORT = 3000 } = process.env` honours PORT exactly and
    contains no such substring, so correct work failed on a spelling it was never
    given (counted seed 440027). Seed-templated copy is exempt: it comes from the
    prompt by construction.
    """
    workspace = ((scenario.get("assertions") or {}).get("workspace") or {}).get("files") or []
    requested = _requested_text(scenario).lower()
    for entry in workspace:
        for needle in entry.get("must_contain") or []:
            literal = str(needle)
            if "{{seed}}" in literal:
                continue
            assert literal.lower() in requested, (
                f"{scenario.get('id')} asserts the literal {literal!r} but never asks for "
                "it — an equally correct spelling would fail a correct build"
            )


@pytest.mark.parametrize("scenario", _scenarios(), ids=lambda s: str(s.get("id")))
def test_every_asserted_file_is_named_in_the_prompt(scenario: dict) -> None:
    workspace = ((scenario.get("assertions") or {}).get("workspace") or {}).get("files") or []
    requested = _requested_text(scenario)
    supplied = _fixture_supplied(scenario)
    for entry in workspace:
        path = entry.get("path")
        if not path or path.startswith(".") or path in _CONVENTIONAL_ENTRYPOINTS:
            continue
        if path in supplied:
            continue
        assert path in requested, (
            f"{scenario.get('id')} asserts {path!r} but never asks for it — "
            "an equally ordinary filename would fail a correct build"
        )
