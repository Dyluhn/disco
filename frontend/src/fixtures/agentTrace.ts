/**
 * A canned agent trace for the Build surface — the "fixtures first" path (offline +
 * tests + screenshots). It exercises the full plan-first lifecycle: a user task → a
 * PROPOSED PLAN that pauses for approval → (on approve) tool actions with capstone
 * progress and a HIGH-risk action that PAUSES at the confirmation gate → confirm
 * resumes to a finished run (reject records a denial). A re-plan trace covers
 * re-entering plan mode after the build.
 */

import type { AgentEvent, ConversationState } from "@/types/agent";

export const FIXTURE_CID = "conv_fixture";
export const FIXTURE_TASK = "Write a fizzbuzz script, run it, then delete it to clean up.";

const PLAN_ID = "evt_plan";
const A_WRITE = "evt_a_write";
const A_RUN = "evt_a_run";
const A_RM = "evt_a_rm"; // the gated one
const REPLAN_ID = "evt_replan";

/** The proposed plan, streamed first and paused for approval (plan-first lifecycle). */
export const planEvent: AgentEvent = {
  kind: "plan",
  id: PLAN_ID,
  source: "agent",
  seq: 2,
  summary: "Write a FizzBuzz script, verify it runs, then remove it to leave a clean workspace.",
  steps: [
    { title: "Write fizzbuzz.py with the FizzBuzz logic" },
    { title: "Run it to verify the output" },
    { title: "Remove the script to clean up" },
  ],
  revision: 1,
  context:
    "## Findings\nThe workspace is empty (file_list returned no entries), so this is a fresh task — no diff to plan around.\n\n## Approach\nPython 3 is available in the sandbox. The classic 15-line FizzBuzz fits one file; running it via `python3 fizzbuzz.py` keeps the verification step trivial. The cleanup step is what triggers the per-action gate (rm -rf), which is expected and helps demo the layered approval.",
};

export const planGateState: ConversationState = {
  conversation_id: FIXTURE_CID,
  execution_status: "AWAITING_PLAN_APPROVAL",
  iteration: 0,
  max_iterations: 500,
  last_seq: 2,
  pending_action_id: null,
  pending_plan_id: PLAN_ID,
};

/** Events streamed after the plan is approved, up to the confirmation gate. Includes
 * `plan_step` capstone reports (invisible to the Activity/Terminal/Files views — they
 * only advance the capstone tracker). */
export const traceBeforeGate: AgentEvent[] = [
  {
    kind: "message",
    id: "evt_user",
    source: "user",
    seq: 1,
    message: { role: "user", content: FIXTURE_TASK },
  },
  {
    kind: "action",
    id: "evt_step1_active",
    source: "agent",
    seq: 3,
    thought: "Starting step 1.",
    tool_call: { tool_name: "plan_step", arguments: { index: 1, state: "active" } },
  },
  {
    kind: "action",
    id: A_WRITE,
    source: "agent",
    seq: 4,
    thought: "I'll write fizzbuzz.py to the workspace.",
    tool_call: {
      tool_name: "file_write",
      arguments: {
        path: "fizzbuzz.py",
        content:
          'for i in range(1, 16):\n    if i % 15 == 0:\n        print("FizzBuzz")\n    elif i % 3 == 0:\n        print("Fizz")\n    elif i % 5 == 0:\n        print("Buzz")\n    else:\n        print(i)\n',
      },
    },
  },
  {
    kind: "observation",
    id: "evt_o_write",
    source: "environment",
    seq: 5,
    action_id: A_WRITE,
    tool_result: { tool_name: "file_write", success: true, content: "wrote 92 bytes to fizzbuzz.py" },
  },
  {
    kind: "action",
    id: "evt_step1_done",
    source: "agent",
    seq: 6,
    thought: "Step 1 complete.",
    tool_call: { tool_name: "plan_step", arguments: { index: 1, state: "done" } },
  },
  {
    kind: "action",
    id: A_RUN,
    source: "agent",
    seq: 7,
    thought: "Run it to check the output.",
    tool_call: { tool_name: "shell", arguments: { command: "python3 fizzbuzz.py" } },
  },
  {
    kind: "observation",
    id: "evt_o_run",
    source: "environment",
    seq: 8,
    action_id: A_RUN,
    tool_result: {
      tool_name: "shell",
      success: true,
      content: "1\n2\nFizz\n4\nBuzz\nFizz\n7\n8\nFizz\nBuzz\n11\nFizz\n13\n14\nFizzBuzz",
      structured: { exit_code: 0, timed_out: false },
    },
  },
  {
    kind: "action",
    id: "evt_step2_done",
    source: "agent",
    seq: 9,
    thought: "Step 2 complete.",
    tool_call: { tool_name: "plan_step", arguments: { index: 2, state: "done" } },
  },
  {
    kind: "action",
    id: A_RM,
    source: "agent",
    seq: 10,
    thought: "Clean up by removing the file.",
    tool_call: { tool_name: "shell", arguments: { command: "rm -rf fizzbuzz.py" } },
    self_assessed_risk: "MEDIUM",
    meta: {
      risk_assessment: {
        risk: "HIGH",
        rationale: "recursive force delete (rm -rf)",
        analyzer: "rule_based",
        self_assessed: "MEDIUM",
      },
    },
  },
];

export const gateState: ConversationState = {
  conversation_id: FIXTURE_CID,
  execution_status: "WAITING_FOR_CONFIRMATION",
  iteration: 3,
  max_iterations: 500,
  last_seq: 10,
  pending_action_id: A_RM,
  pending_plan_id: null,
};

/** After APPROVE: the gated action runs, then the agent wraps up. */
export const traceAfterConfirm: AgentEvent[] = [
  {
    kind: "observation",
    id: "evt_o_rm",
    source: "environment",
    seq: 11,
    action_id: A_RM,
    tool_result: { tool_name: "shell", success: true, content: "", structured: { exit_code: 0 } },
  },
  {
    kind: "message",
    id: "evt_agent_final",
    source: "agent",
    seq: 12,
    message: {
      role: "agent",
      content:
        "Done — **fizzbuzz ran correctly** and the file was removed. The output was:\n\n```\n1\n2\nFizz\n…\nFizzBuzz\n```\n\nThe script lives in `fizzbuzz.py`.",
    },
  },
];

/** After REJECT: the denial is recorded, the agent adapts. */
export const traceAfterReject: AgentEvent[] = [
  {
    kind: "agent_error",
    id: "evt_denied",
    source: "system",
    seq: 11,
    action_id: A_RM,
    error: "Action rejected by user.",
  },
  {
    kind: "message",
    id: "evt_agent_final2",
    source: "agent",
    seq: 12,
    message: {
      role: "agent",
      content: "Understood — I left fizzbuzz.py in place. It ran correctly: 1, 2, Fizz, … FizzBuzz.",
    },
  },
];

export const finishedState: ConversationState = {
  conversation_id: FIXTURE_CID,
  execution_status: "FINISHED",
  iteration: 3,
  max_iterations: 500,
  last_seq: 12,
  pending_action_id: null,
  pending_plan_id: null,
};

// ---- the two-way Ask-gate demo (free-form ask_user) ------------------------
// Reached offline when the build task contains "ask" — the agent poses a
// free-form question and parks at AWAITING_USER_QUESTION so the AskPanel is
// clickable without a live model. Answering (steer) resumes to finished.
export const ASK_QUESTION_ID = "evt_ask_question";

export const askQuestionEvent: AgentEvent = {
  kind: "message",
  id: ASK_QUESTION_ID,
  source: "agent",
  seq: 9,
  message: {
    role: "agent",
    content:
      "Before I write the script — should the output go to **stdout** or to a `results.txt` file? " +
      "Either works; it depends on how you want to use it.",
  },
};

export const askGateState: ConversationState = {
  conversation_id: FIXTURE_CID,
  execution_status: "AWAITING_USER_QUESTION",
  iteration: 1,
  max_iterations: 500,
  last_seq: 9,
  pending_action_id: null,
  pending_plan_id: null,
  pending_question_id: ASK_QUESTION_ID,
};

/** Re-entering plan mode after the build: a focused, diff-style revision (revision 2). */
export const replanEvent: AgentEvent = {
  kind: "plan",
  id: REPLAN_ID,
  source: "agent",
  seq: 13,
  summary: "Add a unit test for the FizzBuzz logic without touching the working script.",
  steps: [
    { title: "Add test_fizzbuzz.py with a few assertions" },
    { title: "Run the test to confirm it passes" },
  ],
  revision: 2,
  context:
    "## What changed\nRe-read `fizzbuzz.py` from the prior build — it's the standard 15-line loop, no helper function to import.\n\n## Approach\nWrite a self-contained test that re-implements the FizzBuzz check in `test_fizzbuzz.py` (since the script itself is just a top-level loop, not importable). The original file stays untouched, per the user's instruction.",
};

export const replanGateState: ConversationState = {
  conversation_id: FIXTURE_CID,
  execution_status: "AWAITING_PLAN_APPROVAL",
  iteration: 0,
  max_iterations: 500,
  last_seq: 13,
  pending_action_id: null,
  pending_plan_id: REPLAN_ID,
};
