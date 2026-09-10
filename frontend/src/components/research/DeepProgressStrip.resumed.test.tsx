/**
 * A resumed run counts turns truthfully — and says where its sources came from.
 *
 * L29's `97-resumed-no-checkpoint.png` shows a resumed run's strip at "Turn 1
 * of 8" with nine sources already in the pool, which reads as a miscount. It is
 * not one. The engine builds a fresh `_AgentState` for the continuation
 * (`agent.py:1410`) whose `turns_charged` starts at zero
 * (`_agent_state.py:68`), and every `turn` payload is derived from that counter
 * (`agent.py:1316-1317` → `_progress_events.py:98`), so the continuation really
 * does get its own full turn allowance. What it does NOT restart is the
 * evidence pool: the checkpoint's passages are rebuilt (`execute.py:274`) and
 * admitted exempt from the new source budget (`agent.py:1413`).
 * `test_resumed_run_emits_turn_1_of_the_full_budget_with_the_pool_carried`
 * (retrieval) pins the producer side.
 *
 * So the turn counter stays as the engine reports it and the strip names the
 * carried part instead. The carry itself is derived from the verbatim L29
 * capture `__fixtures__/l29-resume-frames.json`: the `research_checkpoint`
 * (nine passages), the PAUSED it lands with, and the RUNNING the resumed run
 * opens with.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import capture from "@/lib/__fixtures__/l29-resume-frames.json";
import { deriveActivity, deriveStats, type DeepStats } from "@/lib/deepResearchTrace";
import type { ActionEvent, AgentEvent } from "@/types/agent";
import { DeepProgressStrip } from "./DeepProgressStrip";

const RESUME_EVENTS = capture.events as unknown as AgentEvent[];
const T0 = Date.parse("2026-09-02T10:00:00.000Z");

function turnAction(n: number, of: number): ActionEvent {
  return {
    id: "t1",
    kind: "action",
    seq: 31,
    timestamp: new Date(T0).toISOString(),
    thought: "",
    tool_call: { tool_name: "turn", arguments: { n, of, phase: "thinking" } },
  };
}

describe("a resumed run's stats", () => {
  it("carries the checkpoint's pool count once the run reopens", () => {
    const stats = deriveStats(RESUME_EVENTS);
    expect(stats.carriedSources).toBe(9);
  });

  it("claims no carry on a run that never resumed a checkpoint", () => {
    const beforeResume = RESUME_EVENTS.filter(
      (event) => !(event.kind === "status" && event.status === "RUNNING"),
    );
    expect(deriveStats(beforeResume).carriedSources).toBeNull();
    expect(deriveStats([turnAction(1, 8)]).carriedSources).toBeNull();
  });
});

describe("DeepProgressStrip on a resumed run", () => {
  const stats: DeepStats = {
    ...deriveStats(RESUME_EVENTS),
    searches: 0,
    sourcesDiscovered: 9,
  };

  it("shows the fresh turn budget and the carried pool together", () => {
    render(
      <DeepProgressStrip
        brief={null}
        trace={[]}
        stats={stats}
        status="RUNNING"
        activity={deriveActivity([turnAction(1, 8)])}
        followUpStatus={null}
      />,
    );
    // Both numbers, both true: the budget is the continuation's own, the pool
    // is the one Stop kept.
    expect(screen.getAllByText("Turn 1 of 8").length).toBeGreaterThan(0);
    const carried = document.querySelector("[data-dr-carried]")!;
    expect(carried.textContent).toBe("9 retained from earlier research");
  });

  it("says nothing about a carry on a run that started fresh", () => {
    render(
      <DeepProgressStrip
        brief={null}
        trace={[]}
        stats={{ ...stats, carriedSources: null }}
        status="RUNNING"
        activity={deriveActivity([turnAction(1, 8)])}
        followUpStatus={null}
      />,
    );
    expect(document.querySelector("[data-dr-carried]")).toBeNull();
  });
});
