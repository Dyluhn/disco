/**
 * A canned agent trace for the Build surface — the "fixtures first" path (offline +
 * tests + screenshots). It exercises every trace shape: a user task, tool actions with
 * thoughts, observations, and a HIGH-risk action that PAUSES at the confirmation gate
 * (so the confirmation UI + risk rationale render). Confirm resumes to a finished run;
 * reject records a denial.
 */

import type { AgentEvent, ConversationState } from "@/types/agent";

export const FIXTURE_CID = "conv_fixture";
export const FIXTURE_TASK = "Write a fizzbuzz script, run it, then delete it to clean up.";

const A_WRITE = "evt_a_write";
const A_RUN = "evt_a_run";
const A_RM = "evt_a_rm"; // the gated one

/** Events streamed up to the confirmation gate. */
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
    id: A_WRITE,
    source: "agent",
    seq: 2,
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
    seq: 3,
    action_id: A_WRITE,
    tool_result: { tool_name: "file_write", success: true, content: "wrote 92 bytes to fizzbuzz.py" },
  },
  {
    kind: "action",
    id: A_RUN,
    source: "agent",
    seq: 4,
    thought: "Run it to check the output.",
    tool_call: { tool_name: "shell", arguments: { command: "python3 fizzbuzz.py" } },
  },
  {
    kind: "observation",
    id: "evt_o_run",
    source: "environment",
    seq: 5,
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
    id: A_RM,
    source: "agent",
    seq: 6,
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
  last_seq: 6,
  pending_action_id: A_RM,
};

/** After APPROVE: the gated action runs, then the agent wraps up. */
export const traceAfterConfirm: AgentEvent[] = [
  {
    kind: "observation",
    id: "evt_o_rm",
    source: "environment",
    seq: 7,
    action_id: A_RM,
    tool_result: { tool_name: "shell", success: true, content: "", structured: { exit_code: 0 } },
  },
  {
    kind: "message",
    id: "evt_agent_final",
    source: "agent",
    seq: 8,
    message: {
      role: "agent",
      content: "Done — fizzbuzz ran correctly (1, 2, Fizz, … FizzBuzz) and the file was removed.",
    },
  },
];

/** After REJECT: the denial is recorded, the agent adapts. */
export const traceAfterReject: AgentEvent[] = [
  {
    kind: "agent_error",
    id: "evt_denied",
    source: "system",
    seq: 7,
    action_id: A_RM,
    error: "Action rejected by user.",
  },
  {
    kind: "message",
    id: "evt_agent_final2",
    source: "agent",
    seq: 8,
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
  last_seq: 8,
  pending_action_id: null,
};
