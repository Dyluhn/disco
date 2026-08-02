/**
 * Offline fixture machinery for `subscribeConversation`: replays the in-repo
 * agent-trace / deep-research-trace fixtures (including the confirmation-gate
 * pause) when no agent-server is configured (VITE_AGENT_BASE unset — tests,
 * screenshots, demo mode).
 *
 * Extracted from api/agent.ts (module-size decomposition, PKG-12-FE-BUILD/
 * TS-0002). Module-private: nothing here is part of the public `api/agent`
 * surface — only `subscribeConversation` (in api/agent.ts) calls into it.
 */

import {
  askGateState,
  askQuestionEvent,
  clarifyEvent,
  clarifyGateState,
  finishedState,
  gateState,
  planEvent,
  planGateState,
  replanEvent,
  replanGateState,
  traceAfterConfirm,
  traceAfterReject,
  traceBeforeGate,
} from "@/fixtures/agentTrace";
import {
  fixtureFinishedState,
  fixtureInitialState,
  fixturePlan,
  fixtureReport,
  fixtureRunningEvents,
} from "@/fixtures/deepResearchTrace";
import type { ConversationState, WSServerFrame } from "@/types/agent";
import { isSnapshottedDemoProject } from "../projects";
import { fixtureDelay } from "../client";
import type { AgentHandle } from "../agent";

function runningState(): ConversationState {
  return {
    ...gateState,
    execution_status: "RUNNING",
    pending_action_id: null,
    pending_plan_id: null,
  };
}

/** The rehydrated FINISHED state for a resumed snapshotted demo project (see
 * `subscribeFixture`). Minimal but well-formed: the finished workspace snapshot is
 * restored, so the surface shows the finished-handoff region and the release query
 * enables. The `conversation_id` is the resumed cid (not the fresh-run FIXTURE_CID). */
function rehydratedFinishedState(cid: string): ConversationState {
  return {
    conversation_id: cid,
    execution_status: "FINISHED",
    iteration: 0,
    max_iterations: 500,
    last_seq: 0,
    pending_action_id: null,
    pending_plan_id: null,
  };
}

export function subscribeFixture(cid: string, onFrame: (f: WSServerFrame) => void): AgentHandle {
  let cancelled = false;
  let atGate = false; // paused at the per-action confirmation gate
  let atPlan = false; // paused at the plan-approval gate
  let atAsk = false; // paused at the free-form Ask-gate (AWAITING_USER_QUESTION)

  // Offline/demo fidelity: RESUMING a project that has a persisted finished snapshot
  // (a snapshotted demo project in the fixture registry) rehydrates to a FINISHED
  // view — exactly what a live agent does on resume (restore the committed snapshot).
  // This makes the finished-handoff panels (Self-host / Deliverable) mount from the
  // project's release verdict. It fires on the BARE subscribe a resume performs (a
  // resume carries no `kick`); a fresh run uses a non-snapshotted cid, so its live
  // plan→build→finish flow is untouched. Driven by the registry, not a cid allowlist.
  if (isSnapshottedDemoProject(cid)) {
    void (async () => {
      await fixtureDelay(120);
      if (cancelled) return;
      onFrame({ type: "state", state: rehydratedFinishedState(cid) });
    })();
  }

  async function emit(events: typeof traceBeforeGate, final: ConversationState | null) {
    for (const event of events) {
      if (cancelled) return;
      await fixtureDelay(120);
      onFrame({ type: "event", event });
    }
    if (final && !cancelled) onFrame({ type: "state", state: final });
  }

  // Plan-first: a new task (or a re-plan request) proposes a plan and pauses for approval.
  async function propose(event: typeof planEvent, gate: ConversationState) {
    onFrame({ type: "state", state: runningState() });
    if (cancelled) return;
    await fixtureDelay(120);
    onFrame({ type: "event", event });
    if (cancelled) return;
    atPlan = true;
    onFrame({ type: "state", state: gate });
  }

  // After plan approval: run the build trace up to the per-action confirmation gate.
  async function build() {
    onFrame({ type: "state", state: runningState() });
    await emit(traceBeforeGate, null);
    if (cancelled) return;
    atGate = true;
    onFrame({ type: "state", state: gateState });
  }

  // The two-way Ask-gate demo: the agent poses a free-form question and parks at
  // AWAITING_USER_QUESTION so the AskPanel is reachable offline. The user's typed
  // answer (a steer frame) resumes the run to finished.
  async function askDemo() {
    onFrame({ type: "state", state: runningState() });
    if (cancelled) return;
    await fixtureDelay(120);
    onFrame({ type: "event", event: askQuestionEvent });
    if (cancelled) return;
    atAsk = true;
    onFrame({ type: "state", state: askGateState });
  }

  // The pre-plan Clarify gate demo (RP-13): the planner asks several TYPED
  // questions and parks at AWAITING_USER_QUESTION with a clarify card.
  async function clarifyDemo() {
    onFrame({ type: "state", state: runningState() });
    if (cancelled) return;
    await fixtureDelay(120);
    onFrame({ type: "event", event: clarifyEvent });
    if (cancelled) return;
    atAsk = true;
    onFrame({ type: "state", state: clarifyGateState });
  }

  return {
    send: (f) => {
      if (f.type === "send_message")
        // A task mentioning "clarify" routes to the pre-plan Clarify gate demo;
        // "ask" routes to the free-form Ask-gate; everything else follows the
        // normal plan-first flow.
        void (
          /\bclarif/i.test(f.content ?? "")
            ? clarifyDemo()
            : /\bask\b/i.test(f.content ?? "")
              ? askDemo()
              : propose(planEvent, planGateState)
        );
      else if (f.type === "steer" && atAsk) {
        // the answer to the agent's question → resume + finish
        atAsk = false;
        onFrame({ type: "state", state: runningState() });
        void emit(traceAfterConfirm, finishedState);
      } else if (f.type === "approve_plan" && atPlan) {
        atPlan = false;
        void build();
      } else if (f.type === "request_plan") {
        // (Re-)enter plan mode — a focused revision after the build (or a re-propose).
        atGate = false;
        void propose(replanEvent, replanGateState);
      } else if (f.type === "confirm" && atGate) {
        atGate = false;
        onFrame({ type: "state", state: runningState() });
        void emit(traceAfterConfirm, finishedState);
      } else if (f.type === "reject" && atGate) {
        atGate = false;
        onFrame({ type: "state", state: runningState() });
        void emit(traceAfterReject, finishedState);
      } else if (f.type === "cancel") {
        cancelled = true;
        onFrame({ type: "state", state: { ...finishedState, execution_status: "IDLE" } });
      }
    },
    cancel: () => {
      cancelled = true;
    },
  };
}

export function subscribeDeepFixture(onFrame: (f: WSServerFrame) => void): AgentHandle {
  let cancelled = false;
  let atPlan = false;

  async function streamUntilGate() {
    onFrame({ type: "state", state: fixtureInitialState });
    // Send events up to the AWAITING_PLAN_APPROVAL gate (the first 4 events:
    // user msg, RUNNING, plan, gate-status).
    for (let i = 0; i < 4; i++) {
      if (cancelled) return;
      await fixtureDelay(80);
      onFrame({ type: "event", event: fixtureRunningEvents[i] });
    }
    atPlan = true;
  }

  async function streamRunAndReport() {
    // Stream the post-approval events (plan_approved → phases → actions → obs)
    // with small delays so the live progress feel is preserved.
    for (let i = 4; i < fixtureRunningEvents.length; i++) {
      if (cancelled) return;
      await fixtureDelay(90);
      onFrame({ type: "event", event: fixtureRunningEvents[i] });
    }
    if (cancelled) return;
    await fixtureDelay(150);
    onFrame({ type: "event", event: fixtureReport });
    if (cancelled) return;
    onFrame({
      type: "event",
      event: {
        id: "evt_finished",
        kind: "status",
        source: "system",
        seq: 80,
        timestamp: "2026-06-06T12:00:00Z",
        status: "FINISHED",
      },
    });
    onFrame({ type: "state", state: fixtureFinishedState });
  }

  return {
    send: (f) => {
      if (f.type === "send_message") void streamUntilGate();
      else if (f.type === "approve_plan" && atPlan) {
        atPlan = false;
        void streamRunAndReport();
      } else if (f.type === "request_plan") {
        // re-plan: re-emit a (slightly modified) plan and the gate. For
        // fixture simplicity we just bump revision and re-gate; tests can
        // observe the new revision on the next event.
        atPlan = true;
        void (async () => {
          await fixtureDelay(80);
          onFrame({
            type: "event",
            event: {
              ...fixturePlan,
              id: "evt_replan",
              seq: 5,
              revision: 2,
              summary: `Revised plan: ${f.content ?? ""}`,
            },
          });
        })();
      } else if (f.type === "cancel") {
        cancelled = true;
        onFrame({
          type: "state",
          state: { ...fixtureFinishedState, execution_status: "IDLE" },
        });
      }
    },
    cancel: () => {
      cancelled = true;
    },
  };
}
