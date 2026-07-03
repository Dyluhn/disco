import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { QuestionsV2Panel, type QuestionsV2Item } from "./QuestionsV2Panel";

describe("QuestionsV2Panel", () => {
  it("renders options plus free text and submits both", async () => {
    const user = userEvent.setup();
    const onAnswer = vi.fn();
    const items: QuestionsV2Item[] = [
      {
        id: "style",
        question: "Which starting style?",
        options: ["Minimal", "Editorial"],
      },
    ];
    render(<QuestionsV2Panel question="A few details." items={items} onAnswer={onAnswer} />);

    await user.click(screen.getByText("Minimal"));
    await user.type(screen.getByPlaceholderText(/add details/i), "Use a strong hero.");
    await user.click(screen.getByRole("button", { name: /submit answers/i }));

    expect(onAnswer).toHaveBeenCalledTimes(1);
    expect(onAnswer.mock.calls[0][0]).toContain("Option: Minimal");
    expect(onAnswer.mock.calls[0][0]).toContain("Details: Use a strong hero.");
  });

  it("always includes the required fallback options", () => {
    const items: QuestionsV2Item[] = [
      { id: "variation", question: "What should variations explore?", options: ["Tone"] },
    ];
    render(<QuestionsV2Panel question="A few details." items={items} onAnswer={vi.fn()} />);

    expect(screen.getByText("Tone")).toBeInTheDocument();
    expect(screen.getByText("Explore a few options")).toBeInTheDocument();
    expect(screen.getByText("Decide for me")).toBeInTheDocument();
    expect(screen.getByText("Other")).toBeInTheDocument();
  });
});
