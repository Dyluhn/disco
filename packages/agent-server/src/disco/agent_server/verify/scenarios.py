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

_AUTO_ANSWER = (
    "Proceed using your best reasonable judgment. If something is missing or "
    "ambiguous, note the limitation briefly and complete the task — do not ask "
    "further questions."
)

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
        "raw_html_default",  # deliverables must not be raw HTML only
        "procedural_image_provider",  # images must not come from the procedural tier
    ],
    auto_answer=_AUTO_ANSWER,
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
    auto_answer=_AUTO_ANSWER,
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
    auto_answer=_AUTO_ANSWER,
    timeout_s=600,
)


# ---------------------------------------------------------------------------
# steer_then_stop_build
# ---------------------------------------------------------------------------
# Gap #3: exercise backend commands BEYOND send_message + approve_plan. After the
# build starts, the runner sends a `steer` frame (mid-run redirect) and then a
# `stop` frame over the same WS — driving two of the ~17 commands the runner used
# to ignore. The loop must terminalize (a stopped build settles at IDLE), proving
# the steer + stop frames were honored without a silent hang.

steer_then_stop_build = Scenario(
    id="steer_then_stop_build",
    surface="build",
    prompt="Build a small command-line calculator.",
    model_override=None,
    approve_plan=True,
    ws_commands=[
        {"type": "steer", "content": "Add a unit test for the add() function."},
        {"type": "stop"},
    ],
    expect={},  # any terminal status (IDLE after stop, or FINISHED if it raced to done)
    forbid=[],
    auto_answer=_AUTO_ANSWER,
    timeout_s=300,
)


# ---------------------------------------------------------------------------
# research_report_export
# ---------------------------------------------------------------------------
# Gap #54: a Deep Research run whose markdown report is then EXPORTED via
# POST /conversations/{cid}/report/export. Report export bypasses the event-log
# deliverable path, so this is the only scenario that proves the exported bytes
# are real (non-empty, well-formed) rather than a silent blob the harness can't see.

research_report_export = Scenario(
    id="research_report_export",
    surface="deep_research",
    prompt="Give me a short briefing on the state of RISC-V laptops in 2026.",
    model_override=None,
    approve_plan=True,
    report_export="md",
    expect={"terminal_status": "FINISHED"},
    forbid=[],
    auto_answer=_AUTO_ANSWER,
    timeout_s=600,
)


# ---------------------------------------------------------------------------
# appkit_live_golden  (EPIC M — M1 headless live AppKit acceptance)
# ---------------------------------------------------------------------------
# The headless live acceptance for the AppKit golden path: a real `app_create` →
# `verify_appkit_app` end-to-end against a live model + sandbox. Creates a BUILD
# conversation under the strict AppKit allowlist (appkit_mode=True, EPIC F), sends
# the Build first-send brief signal, approves the plan, and then proves:
#   • the run reached FINISHED (no silent hang),
#   • the AppKit mutator `app_create` AND the strict verifier `verify_appkit_app`
#     were both used (expect_tools) — i.e. the golden path actually ran,
#   • the escape hatch (`request_custom_build`) and raw build tools
#     (shell/file_write/code_exec) were NOT used (forbid_tools) — the strict scope held,
#   • the final `verify_appkit_app` verdict PASSED all seven EPIC G structural checks
#     (expect_appkit_verify).
# This is the LIVE tier — it needs a real driver model + sandbox, so it runs on the
# VM-201 nightly/on-demand host, never on the PR critical path (see scenarios_run / the
# evidence-harness workflow). The runner's logic is contract-tested in
# test_disco_verify_runner.py with a FakeVerifyClient (no live server).

appkit_live_golden = Scenario(
    id="appkit_live_golden",
    surface="build",
    prompt=(
        "Build a lead-generation landing page for a small accounting firm called "
        "Ledgerly. It needs a hero section, a services section, an about section, "
        "and a contact form that captures name, email, and message as leads, plus a "
        "token-protected admin page to review submitted leads."
    ),
    model_override=None,
    appkit_mode=True,
    send_build_brief=True,
    approve_plan=True,
    expect={"terminal_status": "FINISHED"},
    expect_tools=["app_create", "verify_appkit_app"],
    forbid_tools=[
        "request_custom_build",  # the escape hatch — must NOT widen out of AppKit
        "shell",
        "shell_exec",
        "code_exec",
        "file_write",
        "file_edit",
    ],
    expect_appkit_verify=True,
    auto_answer=_AUTO_ANSWER,
    timeout_s=1200,
)
