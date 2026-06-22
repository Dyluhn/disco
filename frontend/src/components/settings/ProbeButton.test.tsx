/**
 * ProbeButton — the shared live "Test" affordance behind all five T4 Settings
 * probes. Drives the real component (no mocks): idle → testing → honest result,
 * a thrown probe error → "failed", and the disabled (no-false-affordance) state.
 * The chip exposes data-probe-status / data-probe-ok so a probe's truth is
 * assertable here and in Playwright.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { describe, expect, it } from "vitest";
import type { ProbeResult } from "@/types/probe";
import { ProbeButton } from "./ProbeButton";

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("ProbeButton", () => {
  it("idle → testing → ok renders a green, honest result from the probe", async () => {
    const d = deferred<ProbeResult>();
    render(
      createElement(ProbeButton, { control: "settings.test", run: () => d.promise }),
    );
    const btn = screen.getByRole("button", { name: /test/i });
    const status = () => document.querySelector("[data-probe-status]")!;
    expect(status()).toHaveAttribute("data-probe-status", "idle");

    fireEvent.click(btn);
    await waitFor(() => expect(status()).toHaveAttribute("data-probe-status", "testing"));
    expect(btn).toBeDisabled(); // can't double-fire while in flight

    d.resolve({ ok: true, status: "ok", detail: "the key works" });
    await waitFor(() => expect(status()).toHaveAttribute("data-probe-status", "ok"));
    expect(status()).toHaveAttribute("data-probe-ok", "true");
    expect(screen.getByText(/the key works/)).toBeInTheDocument();
  });

  it("a not-ok result is shown honestly (no fake green)", async () => {
    const run = async (): Promise<ProbeResult> => ({
      ok: false,
      status: "unauthorized",
      detail: "the key was rejected",
    });
    render(createElement(ProbeButton, { control: "settings.test", run }));
    fireEvent.click(screen.getByRole("button", { name: /test/i }));
    await waitFor(() =>
      expect(document.querySelector("[data-probe-status]")).toHaveAttribute(
        "data-probe-status",
        "unauthorized",
      ),
    );
    expect(document.querySelector("[data-probe-status]")).toHaveAttribute("data-probe-ok", "false");
    expect(screen.getByText(/the key was rejected/)).toBeInTheDocument();
  });

  it("a thrown probe error (endpoint unreachable / 500) surfaces as failed", async () => {
    const run = async (): Promise<ProbeResult> => {
      throw new Error("503 backend down");
    };
    render(createElement(ProbeButton, { control: "settings.test", run }));
    fireEvent.click(screen.getByRole("button", { name: /test/i }));
    await waitFor(() =>
      expect(document.querySelector("[data-probe-status]")).toHaveAttribute(
        "data-probe-status",
        "failed",
      ),
    );
    expect(screen.getByText(/503 backend down/)).toBeInTheDocument();
  });

  it("disabled shows the hint and never calls run (no false affordance)", () => {
    let called = false;
    const run = async (): Promise<ProbeResult> => {
      called = true;
      return { ok: true, status: "ok" };
    };
    render(
      createElement(ProbeButton, {
        control: "settings.test",
        run,
        disabled: true,
        disabledHint: "connect a backend to test",
      }),
    );
    const btn = screen.getByRole("button", { name: /test/i });
    expect(btn).toBeDisabled();
    expect(screen.getByText("connect a backend to test")).toBeInTheDocument();
    fireEvent.click(btn);
    expect(called).toBe(false);
  });
});
