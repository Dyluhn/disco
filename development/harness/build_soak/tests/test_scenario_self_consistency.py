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

# Files a TARGET'S OWN TOOLCHAIN scaffolds, keyed by the toolchain the prompt
# actually names. `npm create vite` really does write package.json and index.html,
# so a React/Vite prompt need not dictate them — but that authority belongs to the
# target, not to the filename.
#
# This was previously a GLOBAL exemption set, `{"index.html", "package.json"}`,
# applied regardless of what the scenario asked for. That let
# `p4_ff_node_restart` require package.json while its prompt asked only for "a
# Node service in server.js": a dependency-free service started with
# `node server.js` has no toolchain that guarantees one, so the requirement was
# unsatisfiable by a correct build. Its siblings p4_ff_node_basic and
# p4_ff_node_pause both say "Include package.json" and are unaffected — which is
# exactly the discrimination a global set cannot make.
_TOOLCHAIN_SCAFFOLDED: tuple[tuple[tuple[str, ...], frozenset[str]], ...] = (
    # (prompt markers that name the toolchain, files that toolchain guarantees)
    (("react", "vite"), frozenset({"package.json", "index.html"})),
    (("next.js", "nextjs"), frozenset({"package.json"})),
)


def _toolchain_guaranteed(scenario: dict) -> frozenset[str]:
    """Files the target named by THIS prompt scaffolds on the model's behalf."""

    text = _requested_text(scenario).lower()
    guaranteed: set[str] = set()
    for markers, files in _TOOLCHAIN_SCAFFOLDED:
        if any(marker in text for marker in markers):
            guaranteed |= set(files)
    return frozenset(guaranteed)


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
    guaranteed = _toolchain_guaranteed(scenario)
    for entry in workspace:
        path = entry.get("path")
        if not path or path.startswith("."):
            continue
        if path in supplied or path in guaranteed:
            continue
        assert path in requested, (
            f"{scenario.get('id')} asserts {path!r} but no authority requires it — "
            "not the prompt, not an import fixture, and not a toolchain this "
            "prompt names. An equally ordinary correct build would fail."
        )


def _synthetic(prompt: str, *paths: str) -> dict:
    return {
        "id": "synthetic",
        "prompt": prompt,
        "assertions": {"workspace": {"files": [{"path": p} for p in paths]}},
    }


def test_dependency_free_node_service_may_be_asserted_without_package_json() -> None:
    """POSITIVE CONTROL: `node server.js` with no dependencies needs no manifest.

    This is the shape that failed counted restart seed 450001. The prompt asks for
    a service in server.js and nothing else; asserting only server.js must be a
    satisfiable contract.
    """

    scenario = _synthetic(
        "Build and browser-verify a Node service in server.js using process.env.PORT.",
        "server.js",
    )

    test_every_asserted_file_is_named_in_the_prompt(scenario)


def test_package_json_is_not_authorised_by_filename_alone() -> None:
    """NEGATIVE CONTROL: the global exemption this replaces would have passed this.

    A plain Node prompt names no toolchain that scaffolds a manifest, so requiring
    one is a requirement with no authority behind it.
    """

    scenario = _synthetic(
        "Build and browser-verify a Node service in server.js using process.env.PORT.",
        "server.js",
        "package.json",
    )

    with pytest.raises(AssertionError, match="no authority requires it"):
        test_every_asserted_file_is_named_in_the_prompt(scenario)


def test_toolchain_named_in_the_prompt_does_authorise_its_scaffolding() -> None:
    """The authority is real when the prompt names the toolchain that provides it.

    `npm create vite` writes package.json and index.html, so a React/Vite prompt
    need not enumerate them — which is why p4_ff_react_steer/continue/restart stay
    valid while p4_ff_node_restart did not.
    """

    scenario = _synthetic(
        "Build and browser-verify a React/Vite dashboard titled 'Dash'.",
        "package.json",
        "index.html",
    )

    test_every_asserted_file_is_named_in_the_prompt(scenario)


def test_a_requested_file_is_still_required_under_the_new_rule() -> None:
    """NEGATIVE CONTROL: narrowing authority must not stop asserting real requests.

    p4_ff_node_basic says "Include package.json"; that requirement is legitimate
    and must survive.
    """

    scenario = _synthetic(
        "Build a Node service in server.js. Include package.json and preview it.",
        "server.js",
        "package.json",
    )

    test_every_asserted_file_is_named_in_the_prompt(scenario)
