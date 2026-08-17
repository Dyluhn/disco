"""Execution-receipt render trailer (TOOL_CALL_THRASH fix).

Success-path exec observations historically rendered only stdout to the model
while the exit status the host already proved lived solely in the observation's
``structured`` payload — so the model re-ran commands purely to learn whether
they succeeded. `ObservationEvent.to_llm_message` now appends one bounded,
deterministic receipt trailer (e.g. ``[exit 0]``) sourced strictly from the
tool's own structured payload. Durable event bytes are unchanged — the receipt
exists only at the render seam.
"""

from disco.core import ObservationEvent, ToolResult, View
from event_fakes import agent_error, user_msg, with_seqs


def _obs(
    content: str,
    structured: dict | None,
    *,
    tool: str = "shell",
) -> ObservationEvent:
    return ObservationEvent(
        tool_result=ToolResult(
            call_id="c",
            tool_name=tool,
            success=True,
            content=content,
            structured=structured,
        ),
        action_id="evt_x",
    )


# ---- positive: the receipt renders from the tool's own structured payload ----


def test_success_exit_zero_renders_receipt():
    msg = _obs("hello from script\n", {"exit_code": 0, "timed_out": False}).to_llm_message()
    assert msg.content == "hello from script\n\n[exit 0]"


def test_nonzero_exit_renders_correct_code():
    msg = _obs("partial output", {"exit_code": 3, "timed_out": False}).to_llm_message()
    assert msg.content.endswith("[exit 3]")
    assert "[exit 0]" not in msg.content


def test_timed_out_reflected_in_receipt():
    msg = _obs("partial", {"exit_code": 124, "timed_out": True}).to_llm_message()
    assert msg.content.endswith("[exit 124, timed out]")


def test_empty_stdout_success_renders_bare_receipt():
    # The exact thrash trigger: a silent script exits 0 and the model saw NOTHING.
    msg = _obs("", {"exit_code": 0, "timed_out": False}).to_llm_message()
    assert msg.content == "[exit 0]"


def test_receipt_survives_the_snip_cap():
    # Trailer is appended AFTER snipping so an oversize output can't destroy it.
    msg = _obs("A" * 10_000, {"exit_code": 0, "timed_out": False}).to_llm_message()
    assert "[snipped" in msg.content
    assert msg.content.endswith("[exit 0]")


def test_receipt_visible_through_view_projection():
    events = with_seqs([user_msg(), _obs("built ok", {"exit_code": 0, "timed_out": False})])
    view = View.of(events)
    assert "[exit 0]" in view.messages[1].content


# ---- negative: no exit status in the payload ⇒ no trailer, ever ----


def test_no_structured_payload_renders_unchanged():
    msg = _obs("plain tool output", None).to_llm_message()
    assert msg.content == "plain tool output"


def test_structured_without_exit_code_renders_unchanged():
    # e.g. a browser/screenshot observation — structured exists, no exit status.
    msg = _obs(
        "screenshot captured",
        {"screenshot_b64": "aGk=", "url": "http://localhost:3000"},
        tool="browser",
    ).to_llm_message()
    assert msg.content == "screenshot captured"


def test_non_integer_exit_code_is_never_fabricated():
    # Strictly sourced from a plausible integer status — never coerced/invented.
    for bad in ("0", 1.0, True, False, None, [0], {"code": 0}):
        msg = _obs("out", {"exit_code": bad}).to_llm_message()
        assert msg.content == "out", f"trailer wrongly rendered for exit_code={bad!r}"


def test_oversize_exit_code_is_bounded_out():
    msg = _obs("out", {"exit_code": 10**12}).to_llm_message()
    assert msg.content == "out"


def test_timed_out_alone_without_exit_code_renders_unchanged():
    msg = _obs("out", {"timed_out": True}).to_llm_message()
    assert msg.content == "out"


# ---- unchanged contracts ----


def test_durable_event_bytes_unchanged():
    # The receipt exists ONLY at the render seam; stored content is untouched.
    obs = _obs("stdout bytes", {"exit_code": 0, "timed_out": False})
    obs.to_llm_message()
    assert obs.tool_result.content == "stdout bytes"
    assert "[exit" not in obs.tool_result.content


def test_failure_path_render_unchanged_no_double_report():
    # Failures are a different event class whose error text already carries
    # "exited N" — its render must not grow a second receipt.
    msg = agent_error(err="command exited 2").to_llm_message()
    assert msg.content == "ERROR: command exited 2"
    assert "[exit" not in msg.content
