/**
 * The steer control is the ONLY user-facing affordance for mid-run steering.
 * Its transport was live end-to-end from D3 while no component rendered a
 * control, so the capability was real for API callers and invisible to users
 * for months. These tests pin the affordance itself so that cannot recur.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { DeepSteerInput } from "./DeepSteerInput";

describe("DeepSteerInput", () => {
  it("sends trimmed steer text and clears the box", async () => {
    const user = userEvent.setup();
    const onSteer = vi.fn();
    render(<DeepSteerInput onSteer={onSteer} />);

    const box = screen.getByLabelText("Steer the research");
    await user.type(box, "  focus on peer-reviewed sources  ");
    await user.click(screen.getByRole("button", { name: "Send steer" }));

    expect(onSteer).toHaveBeenCalledExactlyOnceWith(
      "focus on peer-reviewed sources",
      expect.any(String),
    );
    expect(box).toHaveValue("");
  });

  it("echoes the accepted steer — the agent acts on it at its NEXT turn, not instantly", async () => {
    const user = userEvent.setup();
    render(<DeepSteerInput onSteer={vi.fn()} />);

    await user.type(screen.getByLabelText("Steer the research"), "check 2026 data");
    await user.click(screen.getByRole("button", { name: "Send steer" }));

    expect(screen.getByRole("status")).toHaveTextContent(/Pending delivery.*check 2026 data/i);
    expect(screen.getByRole("status").querySelector("svg")).toBeInTheDocument();
  });

  it("shows a green applied check only for the matching durable fact", async () => {
    const user = userEvent.setup();
    const onSteer = vi.fn();
    const { rerender } = render(<DeepSteerInput onSteer={onSteer} appliedSteerIds={[]} />);
    await user.type(screen.getByLabelText("Steer the research"), "check 2026 data");
    await user.click(screen.getByRole("button", { name: "Send steer" }));
    const steerId = onSteer.mock.calls[0][1];
    expect(steerId).toEqual(expect.any(String));
    expect(screen.getByRole("status")).toHaveTextContent(/Pending delivery/i);

    rerender(<DeepSteerInput onSteer={onSteer} appliedSteerIds={["other-id"]} />);
    expect(screen.getByRole("status")).toHaveTextContent(/Pending delivery/i);
    rerender(<DeepSteerInput onSteer={onSteer} appliedSteerIds={[steerId]} />);
    expect(screen.getByRole("status")).toHaveTextContent(/Applied to a research turn/i);
    expect(screen.getByRole("status").querySelector("svg")).toBeInTheDocument();
  });

  it("never sends empty or whitespace-only guidance", async () => {
    const user = userEvent.setup();
    const onSteer = vi.fn();
    render(<DeepSteerInput onSteer={onSteer} />);

    const send = screen.getByRole("button", { name: "Send steer" });
    expect(send).toBeDisabled();
    await user.type(screen.getByLabelText("Steer the research"), "   ");
    await user.click(send);

    expect(onSteer).not.toHaveBeenCalled();
  });

  it("is inert while disabled", async () => {
    const user = userEvent.setup();
    const onSteer = vi.fn();
    render(<DeepSteerInput onSteer={onSteer} disabled />);

    expect(screen.getByLabelText("Steer the research")).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Send steer" }));
    expect(onSteer).not.toHaveBeenCalled();
  });
});
