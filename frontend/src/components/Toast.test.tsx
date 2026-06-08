/**
 * The toast primitive: shows transient notices, auto-dismisses, and the
 * paid-model cost cue fires ONLY for paid picks (cost honesty, not noise).
 */

import { render, screen, act } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { ToastProvider, useToast } from "@/components/Toast";

function Trigger({ tone, ttlMs }: { tone?: "neutral" | "cost"; ttlMs?: number }) {
  const toast = useToast();
  return (
    <button onClick={() => toast.show({ title: "Now using GPT-X", body: "paid", tone, ttlMs })}>
      go
    </button>
  );
}

describe("Toast", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("shows a toast on demand and auto-dismisses after the ttl", () => {
    render(
      <ToastProvider>
        <Trigger ttlMs={3000} />
      </ToastProvider>,
    );
    expect(screen.queryByText("Now using GPT-X")).not.toBeInTheDocument();
    act(() => screen.getByText("go").click());
    expect(screen.getByText("Now using GPT-X")).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(3100));
    expect(screen.queryByText("Now using GPT-X")).not.toBeInTheDocument();
  });

  it("useToast is a safe no-op without a provider (isolated renders don't crash)", () => {
    // rendering the trigger with NO provider must not throw when clicked
    render(<Trigger />);
    expect(() => act(() => screen.getByText("go").click())).not.toThrow();
  });
});
