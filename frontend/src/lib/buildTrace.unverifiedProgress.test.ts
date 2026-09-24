import { describe, expect, it } from "vitest";
import type { AgentEvent, ConversationStatus } from "@/types/agent";
import { deriveBuildProgress } from "./buildTrace";

const plan = { kind: "plan", id: "plan-2", revision: 2, summary: "mobile fixes",
  steps: Array.from({ length: 6 }, (_, i) => ({ title: `Step ${i + 1}` })) } as AgentEvent;
function status(value: ConversationStatus, detail?: string): AgentEvent {
  return { kind: "status", id: `status-${value}-${detail}`, source: "system", status: value, detail };
}
function verdict(pass: boolean): AgentEvent {
  return { kind: "verifier_verdict", id: `verdict-${pass}`, source: "system",
    artifact_path: "/", artifact_kind: "app", verified: pass, verdict: pass ? "pass" : "fail" };
}
function intent(id: string): AgentEvent {
  return { kind: "workspace_mutation", id: `intent-${id}`, source: "system",
    operation: "agent.run-intent.start", paths: [], run_protocol_version: 1, run_intent_id: id };
}
function progress(rows = [{ index: 1, state: "done" }, { index: 2, state: "active" }]): AgentEvent {
  return { kind: "action", id: "progress", source: "agent", thought: "",
    tool_call: { tool_name: "update_plan_progress", arguments: { steps: rows } } };
}
const warning = { kind: "message", id: "warning", source: "agent",
  message: { role: "assistant", content: "Unverified final result" },
  meta: { host_owned_terminal_warning: true, unverified_release: true } } as AgentEvent;

describe("Finished progress preserves current host verification limits", () => {
  it("replays the six-versus-fourteen G41 failure without inventing six completed steps", () => {
    const events = [intent("current"), plan, progress(Array.from({ length: 14 }, (_, i) => ({
      index: i + 1, state: i < 13 ? "done" : "active",
    }))), verdict(false), status("RUNNING", "unverified_release"), warning, status("FINISHED")];
    expect(deriveBuildProgress(JSON.parse(JSON.stringify(events)), "FINISHED").size).toBe(0);
    // Semantic replay may strip metadata; durable status/verdict retain the limit.
    expect(deriveBuildProgress(events.map((e) => ({ ...e, meta: {} })), "FINISHED").size).toBe(0);
  });

  it.each(["verdict", "marker", "started"])(
    "retains valid explicit progress, stalling active steps with a current %s", (kind) => {
      const evidence = kind === "verdict" ? verdict(false)
        : kind === "marker" ? status("RUNNING", "unverified_release")
          : { kind: "verifier_started", id: "started", source: "system",
            artifact_path: "/", artifact_kind: "app", verifier: "browser" } as AgentEvent;
      expect([...deriveBuildProgress([plan, progress(), evidence, status("FINISHED")], "FINISHED")])
        .toEqual([[1, "done"], [2, "stalled"]]);
    },
  );

  it("later passing host verification supersedes failure and the release marker in the same run", () => {
    const events = [intent("current"), plan, progress(), verdict(false),
      status("RUNNING", "unverified_release"), warning, verdict(true), status("FINISHED")];
    expect([...deriveBuildProgress(events, "FINISHED").values()]).toEqual(Array(6).fill("done"));
  });

  it.each([false, true])("an unbound release marker survives a later pass, started=%s", (started) => {
    const events = [intent("current"), plan, progress(), status("RUNNING", "unverified_release")];
    if (started) events.push({ kind: "verifier_started", id: "later-start", source: "system",
      artifact_path: "/other", artifact_kind: "app", verifier: "browser" } as AgentEvent);
    events.push({ ...verdict(true), artifact_path: "/other" } as AgentEvent, status("FINISHED"));
    expect([...deriveBuildProgress(events, "FINISHED")]).toEqual([[1, "done"], [2, "stalled"]]);
    events.push(intent("new"), status("RUNNING"), verdict(true), status("FINISHED"));
    expect([...deriveBuildProgress(events, "FINISHED").values()]).toEqual(Array(6).fill("done"));
  });

  it.each(["new-intent", "legacy-resume"])(
    "does not carry an old run's failure across %s", (boundary) => {
      const events = [intent("old"), verdict(false), status("FINISHED"),
        boundary === "new-intent" ? intent("new") : status("RUNNING"),
        plan, progress(), status("FINISHED")];
      expect([...deriveBuildProgress(events, "FINISHED").values()]).toEqual(Array(6).fill("done"));
    },
  );

  it("same-intent refresh and ordinary RUNNING statuses do not erase current failed verification", () => {
    const events = [intent("same"), plan, progress(), verdict(false), intent("same"),
      status("RUNNING"), status("FINISHED")];
    expect([...deriveBuildProgress(events, "FINISHED")]).toEqual([[1, "done"], [2, "stalled"]]);
  });

  it("ignores delayed verdicts explicitly bound to an old run intent", () => {
    const foreign = { ...verdict(true), verification_result: { run_intent_id: "old" } } as AgentEvent;
    const events = [intent("current"), plan, progress(), verdict(false), foreign, status("FINISHED")];
    expect([...deriveBuildProgress(events, "FINISHED")]).toEqual([[1, "done"], [2, "stalled"]]);
    const currentPass = { ...verdict(true), verification_result: { run_intent_id: "current" } } as AgentEvent;
    const oldFailure = { ...verdict(false), verification_result: { run_intent_id: "old" } } as AgentEvent;
    expect([...deriveBuildProgress([...events, currentPass, oldFailure], "FINISHED").values()])
      .toEqual(Array(6).fill("done"));
  });

  it("ignores foreign-run start/marker events and unrelated legacy artifact passes", () => {
    const oldStart = { kind: "verifier_started", id: "old-start", source: "system",
      artifact_path: "/", artifact_kind: "app", verifier: "browser", run_intent_id: "old" } as AgentEvent;
    const oldMarker = { ...status("RUNNING", "unverified_release"), run_intent_id: "old" } as AgentEvent;
    const clean = [intent("current"), plan, verdict(true), oldStart, oldMarker, status("FINISHED")];
    expect([...deriveBuildProgress(clean, "FINISHED").values()]).toEqual(Array(6).fill("done"));
    const unrelated = { ...verdict(true), artifact_path: "/other" } as AgentEvent;
    const failed = [intent("current"), plan, progress(), verdict(false), unrelated, status("FINISHED")];
    expect([...deriveBuildProgress(failed, "FINISHED")]).toEqual([[1, "done"], [2, "stalled"]]);
  });

  it.each(["target", "check"])("an unrelated passing %s cannot clear a current failure", (field) => {
    const failed = { ...verdict(false), target_id: "app", check_id: "browser" } as AgentEvent;
    const unrelated = { ...verdict(true), target_id: field === "target" ? "other-app" : "app",
      check_id: field === "check" ? "build" : "browser" } as AgentEvent;
    const events = [intent("current"), plan, progress(), failed, unrelated, status("FINISHED")];
    expect([...deriveBuildProgress(events, "FINISHED")]).toEqual([[1, "done"], [2, "stalled"]]);
    const matching = { ...verdict(true), target_id: "app", check_id: "browser" } as AgentEvent;
    expect([...deriveBuildProgress([...events, matching], "FINISHED").values()])
      .toEqual(Array(6).fill("done"));
  });

  it("an agent-labelled pass or an unverified pass cannot supersede host failure", () => {
    for (const fake of [{ ...verdict(true), source: "agent" }, { ...verdict(true), verified: false }]) {
      const events = [plan, progress(), verdict(false), fake as AgentEvent, status("FINISHED")];
      expect([...deriveBuildProgress(events, "FINISHED")]).toEqual([[1, "done"], [2, "stalled"]]);
    }
  });

  it("keeps RUNNING progress unchanged and clean legacy FINISHED behavior", () => {
    expect([...deriveBuildProgress([plan, progress(), verdict(false)], "RUNNING")])
      .toEqual([[1, "done"], [2, "active"]]);
    expect([...deriveBuildProgress([plan, progress(), status("FINISHED")], "FINISHED").values()])
      .toEqual(Array(6).fill("done"));
  });
});
