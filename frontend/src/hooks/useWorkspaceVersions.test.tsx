import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentEvent, ConversationStatus } from "@/types/agent";

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client");

  async function parse<T>(res: Response): Promise<T> {
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new actual.ApiError(text || `${res.status} ${res.statusText}`, res.status);
    }
    return (await res.json()) as T;
  }

  return {
    ...actual,
    agentLive: () => true,
    agentGet: async <T,>(path: string): Promise<T> =>
      parse<T>(
        await fetch(`http://agent.test${path}`, {
          headers: { accept: "application/json" },
        }),
      ),
    agentSend: async <T,>(method: string, path: string, body?: unknown): Promise<T> =>
      parse<T>(
        await fetch(`http://agent.test${path}`, {
          method,
          headers: body === undefined ? {} : { "content-type": "application/json" },
          body: body === undefined ? undefined : JSON.stringify(body),
        }),
      ),
  };
});

import { restoreWorkspaceVersion } from "@/api/agent";
import { ApiError } from "@/api/client";
import { useWorkspaceVersions } from "./useWorkspaceVersions";

const fetchMock = vi.fn();

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: qc }, children);
}

function restoredEvent(seq: number): AgentEvent {
  return {
    id: `restore-${seq}`,
    kind: "workspace_restored",
    version_seq: seq,
    tree_digest: `digest-${seq}`,
    label: `v${seq}`,
  } as AgentEvent;
}

function statusEvent(status: ConversationStatus): AgentEvent {
  return {
    id: `status-${status}`,
    kind: "status",
    status,
  } as AgentEvent;
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useWorkspaceVersions", () => {
  it("fetches the version list on mount", async () => {
    const versions = [
      {
        seq: 2,
        ts: "2026-07-03T12:00:00Z",
        label: "finished app",
        trigger: "finish",
        file_count: 3,
        total_bytes: 128,
        tree_digest: "abc",
        pinned: false,
      },
    ];
    fetchMock.mockResolvedValueOnce(json({ versions }));

    const { result } = renderHook(() => useWorkspaceVersions("conv_versions"), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => expect(result.current.versions).toEqual(versions));
    expect(result.current.loading).toBe(false);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://agent.test/conversations/conv_versions/versions",
      expect.objectContaining({ headers: { accept: "application/json" } }),
    );
  });

  it("refetches when a workspace_restored event lands", async () => {
    fetchMock.mockImplementation(async () => json({ versions: [] }));

    const { rerender } = renderHook(
      ({ events }) => useWorkspaceVersions("conv_restore", events, "RUNNING"),
      {
        initialProps: { events: [] as AgentEvent[] },
        wrapper: makeWrapper(),
      },
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    rerender({ events: [restoredEvent(4)] });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });

  it("refetches when a terminal status lands", async () => {
    fetchMock.mockImplementation(async () => json({ versions: [] }));

    const { rerender } = renderHook(
      ({ events, status }) => useWorkspaceVersions("conv_terminal", events, status),
      {
        initialProps: {
          events: [] as AgentEvent[],
          status: "RUNNING" as ConversationStatus,
        },
        wrapper: makeWrapper(),
      },
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    rerender({ events: [statusEvent("FINISHED")], status: "FINISHED" });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });

  it("posts a restore and returns the restore result", async () => {
    fetchMock.mockResolvedValueOnce(
      json({ restored: 2, new_version: 3, tree_digest: "new-digest" }),
    );

    await expect(restoreWorkspaceVersion("conv restore", 2)).resolves.toEqual({
      restored: 2,
      new_version: 3,
      tree_digest: "new-digest",
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://agent.test/conversations/conv%20restore/versions/2/restore",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("surfaces a 409 restore response as ApiError", async () => {
    fetchMock.mockResolvedValueOnce(
      json({ detail: { reason: "conversation_running" } }, 409),
    );

    try {
      await restoreWorkspaceVersion("conv_restore", 2);
      throw new Error("restore should have failed");
    } catch (err) {
      expect(err).toBeInstanceOf(ApiError);
      expect((err as ApiError).status).toBe(409);
      expect((err as ApiError).message).toContain("conversation_running");
    }
  });
});
