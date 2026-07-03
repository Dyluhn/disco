/**
 * Gap #14 — a DETERMINISTIC MOUNT SEAM for the model-gated panels.
 *
 * The Confirm / Ask / QuestionsV2 / Clarify / Alternatives gates only render
 * when the LIVE model emits the triggering tool call (risky-action / ask_user /
 * questions_v2 / clarify / 4-consecutive-failures), which makes them
 * flaky-to-impossible to reach in a live run — "the single highest-leverage
 * missing seam" (Opus-A). The panels are
 * pure, prop-driven components that already carry stable `data-disco-control`
 * handles; this harness MOUNTS each one in its gate state ON DEMAND and proves
 * the handle renders + is driveable, with no model in the loop.
 *
 * This is the REGRESSION seam (memory: feedback-live-model-proves-works): it
 * proves the gate panels mount + their handles fire deterministically. The
 * matching live-Playwright path — driving the real /build surface into
 * WAITING_FOR_CONFIRMATION / AWAITING_USER_QUESTION / AWAITING_USER_DECISION — is
 * the proof-tier follow-up and depends on the build hooks (other agents) honoring
 * an injected-state seam.
 *
 * Run: npx vitest run src/lib/gateMountSeam.test.tsx
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ActionEvent, AlternativesEvent } from "@/types/agent";
import { ConfirmationPanel } from "@/components/build/ConfirmationPanel";
import { AskPanel } from "@/components/build/AskPanel";
import { ClarifyPanel, type ClarifyQuestionItem } from "@/components/build/ClarifyPanel";
import { QuestionsV2Panel, type QuestionsV2Item } from "@/components/build/QuestionsV2Panel";
import { AlternativesGate } from "@/components/build/AlternativesGate";

/** A stable risky-action fixture (the WAITING_FOR_CONFIRMATION trigger). */
function confirmActionFixture(): ActionEvent {
  return {
    kind: "action",
    id: "act-risky-1",
    thought: "I need to remove the build directory.",
    tool_call: { tool_name: "shell", arguments: { command: "rm -rf build/" } },
    meta: {
      risk_assessment: {
        risk: "HIGH",
        rationale: "Deletes files irreversibly.",
        analyzer: "policy",
      },
    },
  } as unknown as ActionEvent;
}

/** A stable alternatives fixture (the AWAITING_USER_DECISION trigger). */
function alternativesFixture(): AlternativesEvent {
  return {
    kind: "alternatives",
    id: "alt-1",
    failed_action_id: "act-fail-4",
    summary: "Four attempts to install deps failed. Pick a recovery path.",
    options: [
      { id: "opt-1", title: "Use a lockfile", description: "Install from the committed lockfile.", tool_name: "shell", arguments: { command: "npm ci" } },
      { id: "opt-2", title: "Clear cache", description: "Wipe the npm cache and retry.", tool_name: "shell", arguments: { command: "npm cache clean --force" } },
    ],
  } as unknown as AlternativesEvent;
}

describe("gate mount seam #14 — Confirm gate (WAITING_FOR_CONFIRMATION)", () => {
  it("mounts on demand and both handles fire", async () => {
    const onApprove = vi.fn();
    const onReject = vi.fn();
    const user = userEvent.setup();
    const { container } = render(
      <ConfirmationPanel action={confirmActionFixture()} onApprove={onApprove} onReject={onReject} />,
    );
    const approve = container.querySelector('[data-disco-control="approve-action"]');
    const reject = container.querySelector('[data-disco-control="reject-action"]');
    expect(approve).not.toBeNull();
    expect(reject).not.toBeNull();
    // the risk rationale is in view WITH the decision (the gate's whole point)
    expect(screen.getByText(/Deletes files irreversibly/)).toBeInTheDocument();
    await user.click(approve as Element);
    expect(onApprove).toHaveBeenCalledTimes(1);
    await user.click(reject as Element);
    expect(onReject).toHaveBeenCalledTimes(1);
  });
});

describe("gate mount seam #14 — Ask gate (AWAITING_USER_QUESTION)", () => {
  it("mounts on demand and the answer handle sends the typed reply", async () => {
    const onAnswer = vi.fn();
    const user = userEvent.setup();
    const { container } = render(
      <AskPanel question="Deploy to **staging** or production?" onAnswer={onAnswer} />,
    );
    const handle = container.querySelector('[data-disco-control="answer-question"]');
    expect(handle).not.toBeNull();
    await user.type(screen.getByRole("textbox", { name: /answer the agent/i }), "staging");
    await user.click(handle as Element);
    expect(onAnswer).toHaveBeenCalledWith("staging");
  });
});

describe("gate mount seam #14 — Clarify gate (AWAITING_USER_QUESTION, typed)", () => {
  it("mounts on demand and submit fires once every item is answered", async () => {
    const onAnswer = vi.fn();
    const user = userEvent.setup();
    const items: ClarifyQuestionItem[] = [
      { id: "q1", question: "Target framework?", type: "short_text" },
    ];
    const { container } = render(
      <ClarifyPanel question="A few details before I plan." items={items} onAnswer={onAnswer} />,
    );
    const handle = container.querySelector('[data-disco-control="submit-clarify"]');
    expect(handle).not.toBeNull();
    await user.type(screen.getByLabelText("Target framework?"), "React");
    await user.click(handle as Element);
    expect(onAnswer).toHaveBeenCalledTimes(1);
    expect(onAnswer.mock.calls[0]?.[0]).toContain("React");
  });
});

describe("gate mount seam #14 — Questions v2 gate (AWAITING_USER_QUESTION, structured intake)", () => {
  it("mounts on demand and submit fires once every item is answered", async () => {
    const onAnswer = vi.fn();
    const user = userEvent.setup();
    const items: QuestionsV2Item[] = [
      { id: "style", question: "Starting style?", options: ["Minimal"] },
    ];
    const { container } = render(
      <QuestionsV2Panel question="A few details before I plan." items={items} onAnswer={onAnswer} />,
    );
    const handle = container.querySelector('[data-disco-control="submit-questions-v2"]');
    expect(handle).not.toBeNull();
    await user.click(screen.getByText("Minimal"));
    await user.click(handle as Element);
    expect(onAnswer).toHaveBeenCalledTimes(1);
    expect(onAnswer.mock.calls[0]?.[0]).toContain("Minimal");
  });
});

describe("gate mount seam #14 — Alternatives gate (AWAITING_USER_DECISION)", () => {
  it("mounts on demand and picking a card fires onPick with the option id", async () => {
    const onPick = vi.fn();
    const user = userEvent.setup();
    const { container } = render(
      <AlternativesGate alternatives={alternativesFixture()} onPick={onPick} />,
    );
    // Each recovery card carries a per-option handle `alternative.<optionId>`.
    const handles = container.querySelectorAll('[data-disco-control^="alternative."]');
    expect(handles.length).toBeGreaterThanOrEqual(2);
    const first = container.querySelector('[data-disco-control="alternative.opt-1"]');
    expect(first).not.toBeNull();
    await user.click(first as Element);
    expect(onPick).toHaveBeenCalledWith("opt-1");
  });
});
