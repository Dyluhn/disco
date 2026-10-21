import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";
import type { AgentEvent, ConversationStatus } from "@/types/agent";
import { finishedWorkspaceEvents } from "@/test/workspaceFixtures";
import { useSavedWorkspaceAvailability } from "./useSavedWorkspaceAvailability";

const { listProjects } = vi.hoisted(() => ({ listProjects: vi.fn() }));
vi.mock("@/api/projects", () => ({ listProjects }));

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client }, children);
}
afterEach(() => vi.resetAllMocks());

it("waits for the delayed final seal without treating FINISHED or a checkpoint as a save", () => {
  const completed = finishedWorkspaceEvents("run");
  const { result, rerender } = renderHook(({ events }) =>
    useSavedWorkspaceAvailability(events, "FINISHED", "run"),
  { initialProps: { events: [completed[0]] }, wrapper: wrapper() });
  expect(result.current.hasAvailableWorkspace).toBe(false);
  rerender({ events: [completed[0], { ...completed[1], final_seal: null, trigger: "checkpoint" } as AgentEvent] });
  expect(result.current.hasAvailableWorkspace).toBe(false);
  rerender({ events: completed });
  expect(result.current.hasAvailableWorkspace).toBe(true);
  expect(listProjects).not.toHaveBeenCalled();
});

it("allows a saved live continuation but requires its new seal when it finishes", () => {
  const old = finishedWorkspaceEvents("run");
  const running: AgentEvent = { id: "running-20", seq: 20, kind: "status", source: "system", status: "RUNNING" };
  const current = finishedWorkspaceEvents("run", 30, 2);
  const { result, rerender } = renderHook(({ events, status }) =>
    useSavedWorkspaceAvailability(events, status, "run"),
  { initialProps: { events: [...old, running], status: "RUNNING" as ConversationStatus }, wrapper: wrapper() });
  expect(result.current.hasAvailableWorkspace).toBe(true);
  rerender({ events: [...old, running, current[0]], status: "FINISHED" });
  expect(result.current.hasAvailableWorkspace).toBe(false);
  rerender({ events: [...old, running, ...current], status: "FINISHED" });
  expect(result.current.hasAvailableWorkspace).toBe(true);
});

it.each(["IDLE", "PAUSED", "ERROR"] as const)("preserves owner-scoped legacy saved workspaces in %s", async (status) => {
  listProjects.mockResolvedValue({ projects: [{ id: "legacy", files_missing: false }], status: "ok" });
  const { result } = renderHook(() => useSavedWorkspaceAvailability([], status, "legacy"), { wrapper: wrapper() });
  await waitFor(() => expect(result.current.hasAvailableWorkspace).toBe(true));
});

it("does not let cached import metadata bless an unfinished final capture", async () => {
  listProjects.mockResolvedValue({ projects: [{ id: "legacy", files_missing: false }], status: "ok" });
  const { result, rerender } = renderHook(({ status }) =>
    useSavedWorkspaceAvailability([], status, "legacy"),
  { initialProps: { status: "IDLE" as ConversationStatus }, wrapper: wrapper() });
  await waitFor(() => expect(result.current.hasAvailableWorkspace).toBe(true));
  rerender({ status: "FINISHED" });
  expect(result.current.hasAvailableWorkspace).toBe(false);
});

it("preserves imported files before a run but not as proof of its final capture", () => {
  const imported: AgentEvent = {
    id: "import", kind: "message", source: "environment", message: { role: "user", content: "Imported" },
    meta: { project_import: { workspace_ready: true } },
  };
  const { result, rerender } = renderHook(({ status }) =>
    useSavedWorkspaceAvailability([imported], status, "run"),
  { initialProps: { status: "IDLE" as ConversationStatus }, wrapper: wrapper() });
  expect(result.current.hasAvailableWorkspace).toBe(true);
  rerender({ status: "FINISHED" });
  expect(result.current.hasAvailableWorkspace).toBe(false);
  expect(listProjects).not.toHaveBeenCalled();
});

it.each(["foreign", "untrusted", "later-version", "later-mutation"])("does not offer source for %s evidence", (variant) => {
  const events = finishedWorkspaceEvents(variant === "foreign" ? "different-run" : "run");
  if (variant === "later-version") events.push({
    id: "newer", seq: 12, kind: "workspace_version", source: "system", version_seq: 2,
    tree_digest: "b".repeat(64), trigger: "checkpoint",
  });
  if (variant === "later-mutation") events.push({
    id: "mutation", seq: 12, kind: "workspace_mutation", source: "system", operation: "continue", paths: [],
  });
  const { result } = renderHook(() =>
    useSavedWorkspaceAvailability(events, "FINISHED", "run", variant === "untrusted"),
  { wrapper: wrapper() });
  expect(result.current.hasAvailableWorkspace).toBe(false);
  expect(listProjects).not.toHaveBeenCalled();
});

it("matches persisted effect sequence and rejects a seal for stale effects", () => {
  const effect: AgentEvent = { id: "write", seq: 9, kind: "workspace_mutation", source: "system", operation: "write", paths: ["hello.txt"] };
  const { result, rerender } = renderHook(({ events }) => useSavedWorkspaceAvailability(events, "FINISHED", "run"), {
    initialProps: { events: [effect, ...finishedWorkspaceEvents("run")] }, wrapper: wrapper(),
  });
  expect(result.current.hasAvailableWorkspace).toBe(false);
  rerender({ events: [effect, ...finishedWorkspaceEvents("run", 10, 1, 9)] });
  expect(result.current.hasAvailableWorkspace).toBe(true);
});
