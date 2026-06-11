/**
 * McpSection approval test — rung B.
 *
 * A hash-mismatch server renders the re-approval banner WITH the old-vs-new
 * diff content; confirming fires a REAL POST /api/mcp/servers/{name}/approve.
 * Drives the same real chain as McpSection.live.test.tsx — fetch is the only
 * stub, no vi.mock("@/api/config"). Asserts the diff content and the real
 * request URL/method/body, not a mock count.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as clientModule from "@/api/client";
import { McpSection } from "./McpSection";
import type { McpConnection } from "@/types/config";

const OLD_HASH =
  "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

function seedConnections(): McpConnection[] {
  return [
    {
      id: "changing-srv",
      name: "ChangingServer",
      url: "https://mcp.changing.example.com",
      status: "error",
      transport: "streamable_http",
      risk_tier: "high",
      description_hash: OLD_HASH,
      approved_at: "2026-01-01T00:00:00Z",
      enabled: true,
    },
  ];
}

// ---- fetch router (shared shape with the live test) ------------------------

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: "",
    headers: {
      get: (k: string) => (status === 204 && k === "content-length" ? "0" : null),
    },
    json: async () => body,
    text: async () => (body === undefined ? "" : JSON.stringify(body)),
  } as unknown as Response;
}

let serverState: McpConnection[];

function installFetch() {
  const stub = vi.fn(async (url: string, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    const body = init?.body ? JSON.parse(init.body as string) : undefined;

    if (method === "GET" && url === "/api/mcp") {
      return jsonResponse(serverState.map((c) => ({ ...c })));
    }
    const approveMatch = url.match(/^\/api\/mcp\/servers\/([^/]+)\/approve$/);
    if (method === "POST" && approveMatch) {
      const name = decodeURIComponent(approveMatch[1]);
      const idx = serverState.findIndex((c) => c.id === name);
      if (idx === -1) return jsonResponse({ detail: "not found" }, 404);
      serverState[idx] = {
        ...serverState[idx],
        description_hash: body.description_hash,
        approved_at: "2026-02-02T00:00:00Z",
        status: "connected",
      };
      return jsonResponse(serverState[idx]);
    }
    throw new Error(`unexpected fetch: ${method} ${url}`);
  });
  vi.stubGlobal("fetch", stub);
  return stub;
}

function approveCalls(stub: ReturnType<typeof vi.fn>) {
  return stub.mock.calls.filter(
    ([url, init]) =>
      /\/api\/mcp\/servers\/[^/]+\/approve$/.test(url as string) &&
      (init?.method ?? "").toUpperCase() === "POST",
  );
}

// ---- helpers ---------------------------------------------------------------

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return ({ children }: { children: React.ReactNode }) =>
    createElement(QueryClientProvider, { client: qc }, children);
}

function renderMcp() {
  return render(createElement(McpSection), { wrapper: makeWrapper() });
}

let fetchStub: ReturnType<typeof vi.fn>;

beforeEach(() => {
  serverState = seedConnections();
  vi.spyOn(clientModule, "isLive").mockReturnValue(true);
  fetchStub = installFetch();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// ---- tests -----------------------------------------------------------------

describe("McpSection — approval diff over the real fetch path", () => {
  it("renders the re-approval banner with the hash diff content", async () => {
    const user = userEvent.setup();
    renderMcp();

    await waitFor(() => {
      expect(screen.getByText("ChangingServer")).toBeInTheDocument();
    });

    await user.click(screen.getByText("Re-approve"));

    await waitFor(() => {
      expect(screen.getByText(/Tool descriptions changed/)).toBeInTheDocument();
    });
    // Assert the diff CONTENT, not merely that a banner exists.
    expect(screen.getByText(/Old hash:/)).toBeInTheDocument();
    expect(screen.getByText(/New hash:/)).toBeInTheDocument();
    expect(screen.getAllByText(new RegExp(OLD_HASH)).length).toBeGreaterThanOrEqual(1);
  });

  it("fires a real POST .../approve with the hash when confirmed", async () => {
    const user = userEvent.setup();
    renderMcp();

    await waitFor(() => {
      expect(screen.getByText("ChangingServer")).toBeInTheDocument();
    });

    await user.click(screen.getByText("Re-approve")); // open the diff

    // Confirm (the banner's own "Re-approve" submit button).
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Re-approve$/ })).toBeInTheDocument();
    });
    await user.click(screen.getByRole("button", { name: /Re-approve$/ }));

    await waitFor(() => {
      const calls = approveCalls(fetchStub);
      expect(calls.length).toBe(1);
      const [url, init] = calls[0];
      expect(url).toBe("/api/mcp/servers/changing-srv/approve");
      expect((init as RequestInit).method?.toUpperCase()).toBe("POST");
      expect(JSON.parse((init as RequestInit).body as string)).toEqual({
        description_hash: OLD_HASH,
      });
    });
  });

  it("dismisses the diff without any approve request", async () => {
    const user = userEvent.setup();
    renderMcp();

    await waitFor(() => {
      expect(screen.getByText("ChangingServer")).toBeInTheDocument();
    });

    await user.click(screen.getByText("Re-approve"));
    await waitFor(() => {
      expect(screen.getByText("Dismiss")).toBeInTheDocument();
    });
    await user.click(screen.getByText("Dismiss"));

    await waitFor(() => {
      expect(
        screen.queryByText(/Tool descriptions changed/),
      ).not.toBeInTheDocument();
    });
    expect(approveCalls(fetchStub).length).toBe(0);
  });

  it("shows the transport and risk tier for each server", async () => {
    renderMcp();
    await waitFor(() => {
      expect(screen.getByText("ChangingServer")).toBeInTheDocument();
    });
    expect(screen.getByText(/streamable_http/)).toBeInTheDocument();
    expect(screen.getByText(/high/)).toBeInTheDocument();
  });
});
