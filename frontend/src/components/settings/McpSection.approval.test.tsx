/**
 * McpSection approval test — rung B.
 *
 * A hash-mismatch server renders the re-approval banner WITH the old-vs-new
 * diff content; confirming fires a REAL POST /api/mcp/servers/{name}/approve.
 * Drives the same real chain as McpSection.live.test.tsx — fetch is the only
 * stub, no vi.mock("@/api/config"). Asserts the diff content and the real
 * request URL/method/body, not a mock count.
 *
 * E6 (#10): the seed carries BOTH `description_hash` (the LAST APPROVED hash
 * the operator accepted) and `new_description_hash` (the AUTHORITATIVE
 * fingerprint the live agent-server pool just computed). The diff banner
 * MUST display the two distinct hashes, and the confirm POST MUST carry
 * `new_description_hash` in the body — the operator is accepting the
 * new tool set, so the body is the new hash, not the old one.
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
// E6: the AUTHORITATIVE new hash the live pool computed at startup. Must
// be visually distinct from OLD_HASH so the diff banner is unmistakable.
const NEW_HASH =
  "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff";

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
      new_description_hash: NEW_HASH,
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
let rejectApproval: boolean;

function installFetch() {
  const stub = vi.fn(async (url: string, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    const body = init?.body ? JSON.parse(init.body as string) : undefined;

    if (method === "GET" && url === "/api/mcp") {
      return jsonResponse(serverState.map((c) => ({ ...c })));
    }
    const approveMatch = url.match(/^\/api\/mcp\/servers\/([^/]+)\/approve$/);
    if (method === "POST" && approveMatch) {
      if (rejectApproval) {
        return jsonResponse(
          { detail: "server is disabled; enable it before approving" },
          409,
        );
      }
      const name = decodeURIComponent(approveMatch[1]);
      const idx = serverState.findIndex((c) => c.id === name);
      if (idx === -1) return jsonResponse({ detail: "not found" }, 404);
      // E6: clearing the pending row (the operator accepted the drift) — the
      // app-server returns the row without new_description_hash; subsequent
      // GET /api/mcp must reflect that.
      serverState[idx] =
        body.approval_kind === "config"
          ? {
              ...serverState[idx],
              config_hash: body.config_hash,
              new_config_hash: undefined,
              status: "approval_required",
            }
          : {
              ...serverState[idx],
              description_hash: body.description_hash,
              new_description_hash: undefined,
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
  rejectApproval = false;
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

    await user.click(screen.getByText("Review"));

    await waitFor(() => {
      expect(screen.getByText(/Tool schemas changed/)).toBeInTheDocument();
    });
    // Assert the diff CONTENT, not merely that a banner exists.
    expect(screen.getByText(/Old hash:/)).toBeInTheDocument();
    expect(screen.getByText(/New hash:/)).toBeInTheDocument();
    // E6: the diff MUST surface the AUTHORITATIVE NEW_HASH the live pool
    // computed (not a stub of the stored hash). The OLD_HASH side is the
    // operator's last approval; the NEW_HASH side is the real fingerprint
    // of the changed tool set.
    expect(screen.getAllByText(new RegExp(OLD_HASH)).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(new RegExp(NEW_HASH)).length).toBeGreaterThanOrEqual(1);
  });

  it("fires a real POST .../approve with the NEW hash when confirmed", async () => {
    const user = userEvent.setup();
    renderMcp();

    await waitFor(() => {
      expect(screen.getByText("ChangingServer")).toBeInTheDocument();
    });

    await user.click(screen.getByText("Review")); // open the diff

    // Confirm (the banner's own "Approve" submit button).
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /^Approve$/ })).toBeInTheDocument();
    });
    await user.click(screen.getByRole("button", { name: /^Approve$/ }));

    await waitFor(() => {
      const calls = approveCalls(fetchStub);
      expect(calls.length).toBe(1);
      const [url, init] = calls[0];
      expect(url).toBe("/api/mcp/servers/changing-srv/approve");
      expect((init as RequestInit).method?.toUpperCase()).toBe("POST");
      // E6: the body is NEW_HASH — the operator is accepting the new tool
      // set, so the body MUST be the AUTHORITATIVE new hash the live pool
      // computed, not the OLD/approved hash (which was the stub bug).
      const body = JSON.parse((init as RequestInit).body as string);
      expect(body).toEqual({
        approval_kind: "tools",
        description_hash: NEW_HASH,
      });
      expect(body.description_hash).not.toBe(OLD_HASH);
    });
  });

  it("dismisses the diff without any approve request", async () => {
    const user = userEvent.setup();
    renderMcp();

    await waitFor(() => {
      expect(screen.getByText("ChangingServer")).toBeInTheDocument();
    });

    await user.click(screen.getByText("Review"));
    await waitFor(() => {
      expect(screen.getByText("Dismiss")).toBeInTheDocument();
    });
    await user.click(screen.getByText("Dismiss"));

    await waitFor(() => {
      expect(
        screen.queryByText(/Tool schemas changed/),
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

  it("approves the exact pending server configuration fingerprint", async () => {
    serverState = [
      {
        id: "new-config",
        name: "NewConfig",
        url: "stdio://safe-command",
        status: "approval_required",
        transport: "stdio",
        risk_tier: "medium",
        config_hash: OLD_HASH,
        new_config_hash: NEW_HASH,
        enabled: true,
      },
    ];
    const user = userEvent.setup();
    renderMcp();

    await waitFor(() => expect(screen.getByText("NewConfig")).toBeInTheDocument());
    await user.click(screen.getByText("Review"));
    expect(
      screen.getByText(/Server configuration approval required/),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /^Approve$/ }));

    await waitFor(() => {
      const calls = approveCalls(fetchStub);
      expect(calls.length).toBe(1);
      const body = JSON.parse((calls[0][1] as RequestInit).body as string);
      expect(body).toEqual({
        approval_kind: "config",
        config_hash: NEW_HASH,
      });
    });
  });

  it("surfaces the actionable backend reason when disabled approval is blocked", async () => {
    serverState = [
      {
        id: "disabled-config",
        name: "DisabledConfig",
        url: "https://mcp.example/tools",
        status: "disabled",
        transport: "streamable_http",
        risk_tier: "medium",
        new_config_hash: NEW_HASH,
        enabled: false,
      },
    ];
    rejectApproval = true;
    const user = userEvent.setup();
    renderMcp();

    await waitFor(() => expect(screen.getByText("DisabledConfig")).toBeInTheDocument());
    await user.click(screen.getByText("Review"));
    await user.click(screen.getByRole("button", { name: /^Approve$/ }));

    expect(
      await screen.findByText(/enable it before approving/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Check the App and Agent servers/)).not.toBeInTheDocument();
  });
});
