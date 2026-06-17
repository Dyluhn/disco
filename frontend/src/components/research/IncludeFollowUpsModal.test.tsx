/**
 * IncludeFollowUpsModal — vitest unit tests (WALK-20 / B4).
 *
 * Tests:
 *  (a) Modal renders the follow-up pairs when open
 *  (b) All pairs with answers are selected by default
 *  (c) Pairs without answers are disabled and cannot be toggled
 *  (d) Toggle selects and deselects a pair
 *  (e) Select all / Deselect all works correctly
 *  (f) "Skip" button calls onConfirm([])
 *  (g) "Confirm" button calls onConfirm with the selected seqs
 *  (h) Selection resets to all-selected when modal reopens
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { IncludeFollowUpsModal } from "./IncludeFollowUpsModal";
import type { MessageEvent } from "@/types/agent";

// ── Fixtures ──────────────────────────────────────────────────────────────────

function makeUserMsg(seq: number, content: string): MessageEvent {
  return {
    kind: "message",
    seq,
    source: "user",
    message: { role: "user", content },
  } as MessageEvent;
}

function makeAssistantMsg(seq: number, content: string): MessageEvent {
  return {
    kind: "message",
    seq,
    source: "agent",
    message: { role: "assistant", content },
  } as MessageEvent;
}

// Two complete Q&A pairs + one unanswered question.
const FOLLOW_UPS: MessageEvent[] = [
  makeUserMsg(10, "What about the sparrow?"),
  makeAssistantMsg(11, "The sparrow flies at 4 m/s."),
  makeUserMsg(12, "Any penguins?"),
  makeAssistantMsg(13, "Penguins swim at 6 m/s."),
  makeUserMsg(14, "What about the albatross?"),
  // no assistant answer for seq 14 — still generating
];

// ── Helper ────────────────────────────────────────────────────────────────────

interface RenderOpts {
  open?: boolean;
  followUps?: MessageEvent[];
  onConfirm?: ReturnType<typeof vi.fn>;
  onOpenChange?: ReturnType<typeof vi.fn>;
  actionLabel?: string;
}

function renderModal(opts: RenderOpts = {}) {
  const onConfirm = opts.onConfirm ?? vi.fn();
  const onOpenChange = opts.onOpenChange ?? vi.fn();
  render(
    <IncludeFollowUpsModal
      open={opts.open ?? true}
      onOpenChange={onOpenChange}
      followUps={opts.followUps ?? FOLLOW_UPS}
      onConfirm={onConfirm}
      actionLabel={opts.actionLabel ?? "Export"}
    />,
  );
  return { onConfirm, onOpenChange };
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("IncludeFollowUpsModal", () => {
  // (a) Renders pairs
  it("renders question text for each follow-up pair", () => {
    renderModal();
    expect(screen.getByText(/What about the sparrow/)).toBeInTheDocument();
    expect(screen.getByText(/Any penguins/)).toBeInTheDocument();
    expect(screen.getByText(/What about the albatross/)).toBeInTheDocument();
  });

  // (b) Answered pairs selected by default
  it("pre-selects all pairs that have answers", () => {
    renderModal();
    const buttons = screen.getAllByRole("button", { name: /What about the sparrow|Any penguins/i });
    // The two answered pairs should be aria-pressed=true
    const pressedButtons = screen.getAllByRole("button").filter(
      (b) => b.getAttribute("aria-pressed") === "true",
    );
    expect(pressedButtons).toHaveLength(2);
  });

  // (c) Unanswered pair is disabled
  it("disables the pair without an answer", () => {
    renderModal();
    const allButtons = screen.getAllByRole("button");
    const albatrossBtn = allButtons.find((b) =>
      b.textContent?.includes("What about the albatross"),
    );
    expect(albatrossBtn).toBeDefined();
    expect(albatrossBtn).toBeDisabled();
  });

  // (d) Clicking a pair deselects / selects it
  it("toggles a selected pair off then back on", async () => {
    const user = userEvent.setup();
    renderModal();

    // Find the sparrow button (answered, initially selected)
    const sparrowBtn = screen
      .getAllByRole("button")
      .find((b) => b.textContent?.includes("What about the sparrow"))!;
    expect(sparrowBtn.getAttribute("aria-pressed")).toBe("true");

    await user.click(sparrowBtn);
    expect(sparrowBtn.getAttribute("aria-pressed")).toBe("false");

    await user.click(sparrowBtn);
    expect(sparrowBtn.getAttribute("aria-pressed")).toBe("true");
  });

  // (e) Select all / Deselect all
  it("Deselect all clears selection; Select all restores it", async () => {
    const user = userEvent.setup();
    renderModal();

    // Initially all answered are selected → button says "Deselect all"
    const toggleAll = screen.getByRole("button", { name: /Deselect all/i });
    await user.click(toggleAll);

    // Now says "Select all"; no pair should be pressed
    const selectAll = screen.getByRole("button", { name: /Select all/i });
    const pressedAfterDeselect = screen.getAllByRole("button").filter(
      (b) => b.getAttribute("aria-pressed") === "true",
    );
    expect(pressedAfterDeselect).toHaveLength(0);

    await user.click(selectAll);
    const pressedAfterSelectAll = screen.getAllByRole("button").filter(
      (b) => b.getAttribute("aria-pressed") === "true",
    );
    expect(pressedAfterSelectAll).toHaveLength(2);
  });

  // (f) Skip calls onConfirm([])
  it("Skip button calls onConfirm with an empty array", async () => {
    const user = userEvent.setup();
    const { onConfirm } = renderModal();
    await user.click(screen.getByRole("button", { name: /Skip/i }));
    expect(onConfirm).toHaveBeenCalledOnce();
    expect(onConfirm).toHaveBeenCalledWith([]);
  });

  // (g) Confirm threads selected seqs
  it("Confirm button calls onConfirm with the selected question seqs", async () => {
    const user = userEvent.setup();
    const { onConfirm } = renderModal();

    // Both answered pairs pre-selected (seqs 10, 12).
    const confirmBtn = screen.getByRole("button", { name: /Export with 2 follow-ups/i });
    await user.click(confirmBtn);

    expect(onConfirm).toHaveBeenCalledOnce();
    const [seqs] = onConfirm.mock.calls[0] as [number[]];
    expect(seqs).toHaveLength(2);
    expect(seqs).toContain(10);
    expect(seqs).toContain(12);
  });

  // (g2) Confirm after deselecting one
  it("Confirm with one deselected pair passes only the remaining seq", async () => {
    const user = userEvent.setup();
    const { onConfirm } = renderModal();

    const sparrowBtn = screen
      .getAllByRole("button")
      .find((b) => b.textContent?.includes("What about the sparrow"))!;
    await user.click(sparrowBtn); // deselect seq 10

    const confirmBtn = screen.getByRole("button", { name: /Export with 1 follow-up/i });
    await user.click(confirmBtn);

    const [seqs] = onConfirm.mock.calls[0] as [number[]];
    expect(seqs).toHaveLength(1);
    expect(seqs).toContain(12);
    expect(seqs).not.toContain(10);
  });

  // (g3) When all deselected, confirm says "without follow-ups"
  it("Confirm label says 'without follow-ups' when none selected", async () => {
    const user = userEvent.setup();
    const { onConfirm } = renderModal();

    await user.click(screen.getByRole("button", { name: /Deselect all/i }));
    const confirmBtn = screen.getByRole("button", { name: /Export without follow-ups/i });
    await user.click(confirmBtn);

    expect(onConfirm).toHaveBeenCalledWith([]);
  });

  // (h) Modal not rendered when closed
  it("does not render when open=false", () => {
    renderModal({ open: false });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});
