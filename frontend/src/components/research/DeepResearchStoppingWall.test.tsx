/**
 * The stopping wall on screen, rendered from the same verbatim capture the copy
 * test drives (`lib/__fixtures__/l29-stop-frames.json` — real WebSocket frames
 * from the live L29 run, not a hand-written stand-in).
 *
 * What this pins is the pair of rules the measured defect broke: the wall is up
 * from the Stop marker until the run writes its own ending, and it is composed
 * only from events — no spinner, no timer-driven text, no state the system has
 * not observed.
 *
 * Run: npx vitest run src/components/research/DeepResearchStoppingWall.test.tsx
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import capture from "@/lib/__fixtures__/l29-stop-frames.json";
import { deriveActivity } from "@/lib/deepResearchTrace";
import type { AgentEvent } from "@/types/agent";
import { DeepResearchStoppingWall } from "./DeepResearchStoppingWall";

const events = capture.events as unknown as AgentEvent[];
const beforeTerminal = events.filter((event) => event.kind !== "status");
const CAPTURED_AT = Date.parse("2026-09-02T08:54:54.000Z");
const SOURCES = capture.measured_over_the_whole_run.sources_discovered;

function renderWall(list: AgentEvent[], onKill = vi.fn()) {
  return render(
    <DeepResearchStoppingWall
      activity={deriveActivity(list)}
      nowMs={CAPTURED_AT}
      sourcesDiscovered={SOURCES}
      onKill={onKill}
    />,
  );
}

describe("DeepResearchStoppingWall", () => {
  it("shows the four parts while the run has not answered", () => {
    const { container } = renderWall(beforeTerminal);
    const panel = container.querySelector('[data-dr-stopping="panel"]');
    expect(panel).not.toBeNull();
    const text = (panel as HTMLElement).textContent ?? "";
    expect(text).toMatch(/You pressed Stop at /); // why
    expect(text).toContain("Writing the report"); // state
    expect(text).toContain(`the ${SOURCES} sources it has found are kept`); // next
    expect(text).toContain("Kill ends the run immediately"); // allowed
    // The one destructive option is the only control here, and it is real.
    expect(screen.getByRole("button", { name: /kill the run now/i })).toBeEnabled();
  });

  it("renders nothing once the run writes its own ending", () => {
    // The capture's last frame is the terminal PAUSED/"stopped"; the checkpoint
    // panel with Resume takes over from there, unchanged.
    const { container } = renderWall(events);
    expect(container.querySelector('[data-dr-stopping="panel"]')).toBeNull();
  });

  it("never fakes liveness: no spinner anywhere in the wall", () => {
    const { container } = renderWall(beforeTerminal);
    expect(container.querySelectorAll(".animate-spin")).toHaveLength(0);
  });

  it("ages only against reported events — a later clock moves the words", () => {
    const { container } = renderWall(beforeTerminal);
    const first = container.querySelector('[data-dr-stopping="state"]')?.textContent ?? "";
    expect(first).toContain("Still reporting");

    // Two minutes later with nothing new reported, the signal says so rather
    // than continuing to claim the run is producing.
    const later = render(
      <DeepResearchStoppingWall
        activity={deriveActivity(beforeTerminal)}
        nowMs={CAPTURED_AT + 120_000}
        sourcesDiscovered={SOURCES}
        onKill={vi.fn()}
      />,
    );
    const aged =
      later.container.querySelector('[data-dr-stopping="state"]')?.textContent ?? "";
    expect(aged).toContain("No signal for");
  });
});
