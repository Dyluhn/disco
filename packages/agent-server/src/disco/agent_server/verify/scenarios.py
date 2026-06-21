"""Pre-defined disco-verify scenarios (W15).

Import these objects and pass them directly to ``run_scenario``::

    from disco.agent_server.verify.scenarios import slides_from_research_report
    from disco.agent_server.verify.runner import run_scenario

    result = await run_scenario(slides_from_research_report)

Each scenario encodes exactly what to submit to the app and which invariants
to check — the harness fills in the evidence and decides passed/failed.
"""

from __future__ import annotations

from .schema import Scenario

# ---------------------------------------------------------------------------
# slides_from_research_report
# ---------------------------------------------------------------------------
# The canonical "slides" scenario from the codex round-1 / campaign design.
# Checks that the default slides deliverable is a presentable format (pptx/pdf)
# and that neither raw HTML nor procedural placeholder images are produced.

slides_from_research_report = Scenario(
    id="slides_from_research_report",
    surface="agent",
    prompt="Make slides from this research report",
    model_override=None,
    approve_plan=True,  # approve the plan so the build actually runs to a deliverable
    expect={
        "terminal_status": "FINISHED",
        "deliverable_type": "deck",
    },
    forbid=[
        "raw_html_default",        # deliverables must not be raw HTML only
        "procedural_image_provider",  # images must not come from the procedural tier
    ],
    timeout_s=600,
)

# ---------------------------------------------------------------------------
# missing_file_sandbox_error
# ---------------------------------------------------------------------------
# Exercises the W1/W2 contract LIVE: a build that touches a nonexistent file must reach a
# TERMINAL state (never hang at RUNNING). A missing-file READ through the agent's own tool is
# handled gracefully (the agent is told the file is absent and finishes), so the realistic
# terminal here is FINISHED — the invariant under test is "the loop terminalizes, no silent
# hang." (The crash→ERROR path of W1/W2 — an uncaught SandboxError in the loop's own
# bookkeeping reads — is proven deterministically by the W1/W2 unit contracts.)

missing_file_sandbox_error = Scenario(
    id="missing_file_sandbox_error",
    surface="build",
    prompt="Read the workspace file missing_notes_99999.txt and summarise it",
    model_override=None,
    approve_plan=True,  # approve so the build runs and actually touches the missing file
    expect={
        "terminal_status": "FINISHED",
    },
    forbid=[],
    timeout_s=180,
)


# ---------------------------------------------------------------------------
# app_from_build
# ---------------------------------------------------------------------------
# codex round-5: the Build surface's PRIMARY deliverable for "make me a website" is a live-app
# handoff (artifact_kind="app"). This scenario exercises that input→output boundary: the run
# must FINISH and actually produce an app deliverable (its preview URL, if any, must be live).

app_from_build = Scenario(
    id="app_from_build",
    surface="build",
    prompt="Build a simple one-page landing page website and serve it.",
    model_override=None,
    approve_plan=True,
    expect={
        "terminal_status": "FINISHED",
        "deliverable_type": "app",
    },
    forbid=[],
    timeout_s=600,
)
