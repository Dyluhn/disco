/**
 * The Clarify gate panel: renders typed pre-plan clarification questions.
 *
 * Regression coverage for fix-clarify-cards — the live build "Make a simple
 * macosx clone" (2026-06-24) rendered "empty bubble cards": choice questions
 * whose options were empty placeholders showed as blank, un-pickable radios.
 * Defense in depth (mirroring the backend handle_clarify normalization): empty
 * option labels are dropped and a choice with <2 real options falls back to a
 * free-text box so a malformed/old event never strands the user.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ClarifyPanel } from "@/components/build/ClarifyPanel";
import type { ClarifyQuestionItem } from "@/components/build/ClarifyPanel";

describe("ClarifyPanel — typed clarification gate", () => {
  it("renders a real choice question as radio options and submits the picks", async () => {
    const onAnswer = vi.fn();
    const user = userEvent.setup();
    const items: ClarifyQuestionItem[] = [
      { id: "stack", question: "React or Vue?", type: "choice", options: ["React", "Vue"] },
    ];
    render(<ClarifyPanel question="A couple of choices." items={items} onAnswer={onAnswer} />);

    await user.click(screen.getByText("React"));
    await user.click(screen.getByRole("button", { name: /submit answers/i }));
    expect(onAnswer).toHaveBeenCalledTimes(1);
    expect(onAnswer.mock.calls[0][0]).toContain("React");
  });

  it("BUG 2 — a choice with only EMPTY options falls back to a text box (no blank radios)", async () => {
    const onAnswer = vi.fn();
    const user = userEvent.setup();
    const items: ClarifyQuestionItem[] = [
      { id: "scope", question: "How ambitious?", type: "choice", options: ["", "", ""] },
    ];
    render(<ClarifyPanel question="A few choices." items={items} onAnswer={onAnswer} />);

    // No radio buttons rendered — the empty options must NOT become blank radios.
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
    // A usable text input is shown instead, so the user is never stranded.
    const box = screen.getByLabelText("How ambitious?");
    expect(box).toBeInTheDocument();
    await user.type(box, "small");
    await user.click(screen.getByRole("button", { name: /submit answers/i }));
    expect(onAnswer).toHaveBeenCalledTimes(1);
    expect(onAnswer.mock.calls[0][0]).toContain("small");
  });

  it("drops blank labels but keeps a real choice when >=2 options survive", () => {
    const items: ClarifyQuestionItem[] = [
      { id: "look", question: "Which look?", type: "choice", options: ["Modern", "", "Classic"] },
    ];
    render(<ClarifyPanel question="Pick a look." items={items} onAnswer={vi.fn()} />);
    const radios = screen.getAllByRole("radio");
    expect(radios).toHaveLength(2); // the empty middle option is dropped
    expect(screen.getByText("Modern")).toBeInTheDocument();
    expect(screen.getByText("Classic")).toBeInTheDocument();
  });
});
