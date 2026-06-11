/**
 * McpSection live test — rung B.
 *
 * Drives the REAL data path: McpSection → useConfig hooks → api/config.ts →
 * client.ts apiGet/apiSend → fetch(...). The network boundary is the ONLY thing
 * stubbed (vi.stubGlobal("fetch")), exactly as the repo's agent.upload.test.ts
 * does. Live mode is forced by spying isLive() (BASE is captured at module-load,
 * so env stubbing would be too late). Assertions pin the real URL + method +
 * JSON body — change api/config.ts's endpoint to garbage and these go red.
 *
 * NO vi.mock("@/api/config") — module-mocking the data layer would let the test
 * pass even if the fetch wiring or the /api/mcp contract were broken (the
 * anti-gaming bar's vacuous-test REJECT).
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as clientModule from "@/api/client";
import { McpSection } from "./McpSection";
import type { McpConnection } from "@/types/config";

// ---- the contract the server emits (proven by test_mcp_endpoints.py) --------

function seedConnections(): McpConnection[] {
  return [
    {
      id: "fs",
      name: "Filesystem",
      url: "stdio://mcp-fs",
      status: "connected",
      transport: "stdio",
      risk_tier: "high",
      description_hash:
        "abc123def456abc123def456abc123def456abc123def456abc123def456abc1",
      approved_at: "2026-01-01T00:00:00Z",
      enabled: true,
    },
    {
      id: "gh",
      name: "GitHub",
      url: "https://mcp.github.local",
      status: "disconnected",
      transport: "streamable_http",
      risk_tier: "medium",
      enabled: true,
    },
  ];
}

// ---- a stateful fetch router (the "server"), shaped like client.ts expects --

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

/** State held by the fake server so a mutation is visible on the next GET. */
let serverState: McpConnection[];

function installFetch() {
  const stub = vi.fn(async (url: string, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    const body = init?.body ? JSON.parse(init.body as string) : undefined;

    if (method === "GET" && url === "/api/mcp") {
      return jsonResponse(serverState.map((c) => ({ ...c })));
    }
    if (method === "POST" && url === "/api/mcp/servers") {
      const conn: McpConnection = {
        id: body.name,
        name: body.name,
        url: body.url,
        status: "disconnected",
        transport: body.transport,
        risk_tier: body.risk_tier,
        enabled: true,
      };
      serverState.push(conn);
      return jsonResponse(conn);
    }
    const rowMatch = url.match(/^\/api\/mcp\/servers\/([^/]+)$/);
    if (method === "PATCH" && rowMatch) {
      const name = decodeURIComponent(rowMatch[1]);
      const idx = serverState.findIndex((c) => c.id === name);
      if (idx === -1) return jsonResponse({ detail: "not found" }, 404);
      serverState[idx] = { ...serverState[idx], ...body };
      return jsonResponse(serverState[idx]);
    }
    if (method === "DELETE" && rowMatch) {
      const name = decodeURIComponent(rowMatch[1]);
      serverState = serverState.filter((c) => c.id !== name);
      return jsonResponse(undefined, 204);
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

/** All GET /api/mcp requests (initial load + post-mutation invalidations). */
function listGets(stub: ReturnType<typeof vi.fn>) {
  return stub.mock.calls.filter(
    ([url, init]) => (init?.method ?? "GET").toUpperCase() === "GET" && url === "/api/mcp",
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

// ---- setup / teardown ------------------------------------------------------

let fetchStub: ReturnType<typeof vi.fn>;

beforeEach(() => {
  serverState = seedConnections();
  // Force the live branch in api/config.ts (BASE is captured at module load, so
  // spying the isLive() function is the only reliable seam — same approach as
  // agent.upload.test.ts spying agentLive()).
  vi.spyOn(clientModule, "isLive").mockReturnValue(true);
  fetchStub = installFetch();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// ---- tests -----------------------------------------------------------------

describe("McpSection — live CRUD over the real fetch path", () => {
  it("renders the connection list from a real GET /api/mcp", async () => {
    renderMcp();
    await waitFor(() => {
      expect(screen.getByText("Filesystem")).toBeInTheDocument();
    });
    expect(screen.getByText("GitHub")).toBeInTheDocument();

    // The list came from a real GET against the contract endpoint.
    expect(listGets(fetchStub).length).toBe(1);

    // No leftover wiring-pending affordances.
    expect(screen.queryByText(/WIRING-PENDING/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/pending/i)).not.toBeInTheDocument();
    const addBtn = screen.getByRole("button", { name: /add connection/i });
    expect(addBtn).not.toBeDisabled();
  });

  it("POSTs the form values to /api/mcp/servers and re-fetches the list", async () => {
    const user = userEvent.setup();
    renderMcp();

    await waitFor(() => {
      expect(screen.getByText("Filesystem")).toBeInTheDocument();
    });
    expect(listGets(fetchStub).length).toBe(1);

    await user.click(screen.getByRole("button", { name: /add connection/i }));
    await user.type(screen.getByLabelText("MCP server name"), "MyServer");
    await user.type(screen.getByLabelText("MCP server URL"), "https://my.mcp.server");
    await user.click(screen.getByRole("button", { name: /add connection/i }));

    // Assert the REAL request: URL + method + parsed JSON body.
    await waitFor(() => {
      const post = fetchStub.mock.calls.find(
        ([url, init]) =>
          url === "/api/mcp/servers" && init?.method?.toUpperCase() === "POST",
      );
      expect(post).toBeTruthy();
      expect(
        (post![1] as RequestInit).headers as Record<string, string>,
      ).toMatchObject({ "content-type": "application/json" });
      expect(JSON.parse((post![1] as RequestInit).body as string)).toMatchObject({
        name: "MyServer",
        url: "https://my.mcp.server",
        transport: "streamable_http",
        risk_tier: "medium",
        enabled: true,
      });
    });

    // The list query was invalidated → a second GET, and the new row renders
    // from the (stateful) server's response — not from any client-side optimism.
    await waitFor(() => {
      expect(listGets(fetchStub).length).toBe(2);
    });
    expect(await screen.findByText("MyServer")).toBeInTheDocument();
  });

  it("toggles enabled via a real PATCH /api/mcp/servers/{name}", async () => {
    const user = userEvent.setup();
    renderMcp();

    await waitFor(() => {
      expect(screen.getByText("Filesystem")).toBeInTheDocument();
    });

    const fsSwitch = screen.getAllByRole("switch")[0]; // Filesystem, enabled=true
    expect(fsSwitch).toHaveAttribute("aria-checked", "true");
    await user.click(fsSwitch);

    await waitFor(() => {
      const patch = fetchStub.mock.calls.find(
        ([url, init]) =>
          url === "/api/mcp/servers/fs" && init?.method?.toUpperCase() === "PATCH",
      );
      expect(patch).toBeTruthy();
      expect(JSON.parse((patch![1] as RequestInit).body as string)).toMatchObject({
        name: "fs",
        enabled: false,
      });
    });
  });

  it("removes a server via a real DELETE /api/mcp/servers/{name}", async () => {
    const user = userEvent.setup();
    renderMcp();

    await waitFor(() => {
      expect(screen.getByText("Filesystem")).toBeInTheDocument();
    });

    await user.click(screen.getAllByLabelText(/remove/i)[0]); // Filesystem

    await waitFor(() => {
      const del = fetchStub.mock.calls.find(
        ([url, init]) =>
          url === "/api/mcp/servers/fs" && init?.method?.toUpperCase() === "DELETE",
      );
      expect(del).toBeTruthy();
    });

    // The row is gone after the post-delete re-fetch returns the smaller list.
    await waitFor(() => {
      expect(screen.queryByText("Filesystem")).not.toBeInTheDocument();
    });
    expect(screen.getByText("GitHub")).toBeInTheDocument();
  });

  it("shows the Re-approve control and SHA-256 fingerprint for hashed connections", async () => {
    renderMcp();
    await waitFor(() => {
      expect(screen.getByText("Filesystem")).toBeInTheDocument();
    });

    expect(screen.getAllByText("Re-approve").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/abc123def456…/)).toBeInTheDocument();
    expect(screen.getByText(/\(SHA-256\)/)).toBeInTheDocument();
  });

  it("applies STATUS_META labels from the server's status field", async () => {
    renderMcp();
    await waitFor(() => {
      expect(screen.getByText("Filesystem")).toBeInTheDocument();
    });
    expect(screen.getByText("Connected")).toBeInTheDocument();
    expect(screen.getByText("Disconnected")).toBeInTheDocument();
  });
});
