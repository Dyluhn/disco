/**
 * The two-way Ask-gate panel: renders the agent's free-form question and an
 * answer box whose submit (button or Enter) sends the typed answer — the loop's
 * resume signal.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { AskPanel } from "@/components/build/AskPanel";

describe("AskPanel — free-form Ask-gate", () => {
  it("renders the question and sends the typed answer", async () => {
    const onAnswer = vi.fn();
    const user = userEvent.setup();
    render(
      <AskPanel
        question="Should I deploy to **staging** or production?"
        onAnswer={onAnswer}
      />,
    );
    // the question is shown (Markdown rendered — bold becomes <strong>)
    expect(screen.getByText(/Should I deploy to/i)).toBeInTheDocument();
    expect(screen.getByText("staging").tagName).toBe("STRONG");

    const box = screen.getByRole("textbox", { name: /answer the agent/i });
    await user.type(box, "staging first");
    await user.click(screen.getByRole("button", { name: /send answer/i }));
    expect(onAnswer).toHaveBeenCalledWith("staging first");
  });

  it("Enter sends; Shift+Enter does not", async () => {
    const onAnswer = vi.fn();
    const user = userEvent.setup();
    render(<AskPanel question="Pick one?" onAnswer={onAnswer} />);
    const box = screen.getByRole("textbox", { name: /answer the agent/i });

    await user.type(box, "the first option{Shift>}{Enter}{/Shift}");
    expect(onAnswer).not.toHaveBeenCalled(); // Shift+Enter is a newline, not send

    await user.type(box, "{Enter}");
    expect(onAnswer).toHaveBeenCalledTimes(1);
    expect(onAnswer.mock.calls[0][0]).toContain("the first option");
  });

  it("does not send an empty answer", async () => {
    const onAnswer = vi.fn();
    const user = userEvent.setup();
    render(<AskPanel question="Anything?" onAnswer={onAnswer} />);
    await user.click(screen.getByRole("button", { name: /send answer/i }));
    expect(onAnswer).not.toHaveBeenCalled();
  });
});
