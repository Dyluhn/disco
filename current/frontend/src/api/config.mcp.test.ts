import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  agentGet: vi.fn(),
  agentLive: vi.fn(),
  agentSend: vi.fn(),
  apiGet: vi.fn(),
  apiSend: vi.fn(),
  fixtureDelay: vi.fn(),
  isLive: vi.fn(),
}));

vi.mock("./client", () => mocks);

import { listMcpConnections, revokeMcpServer } from "./config";

describe("MCP status projection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.isLive.mockReturnValue(true);
    mocks.agentLive.mockReturnValue(true);
    mocks.apiGet.mockResolvedValue([
      {
        id: "reachable",
        name: "reachable",
        url: "https://reachable.example/mcp",
        enabled: true,
        status: "connected",
      },
      {
        id: "off",
        name: "off",
        url: "https://off.example/mcp",
        enabled: false,
        status: "connected",
      },
    ]);
  });

  it("uses runtime truth and never labels a disabled row connected", async () => {
    mocks.agentGet.mockResolvedValue({
      enabled: true,
      servers: {
        reachable: { status: "degraded" },
        off: { status: "connected" },
      },
    });

    const connections = await listMcpConnections();

    expect(mocks.agentGet).toHaveBeenCalledWith("/api/mcp/servers");
    expect(connections.map(({ id, status }) => ({ id, status }))).toEqual([
      { id: "reachable", status: "degraded" },
      { id: "off", status: "disabled" },
    ]);
  });

  it("projects every row disabled when the global runtime gate is off", async () => {
    mocks.agentGet.mockResolvedValue({
      enabled: false,
      servers: { reachable: { status: "connected" } },
    });

    const connections = await listMcpConnections();

    expect(connections.every((connection) => connection.status === "disabled")).toBe(true);
  });

  it("revokes through the app boundary and hot-reloads the agent", async () => {
    mocks.apiSend.mockResolvedValue({
      id: "reachable",
      name: "reachable",
      status: "approval_required",
    });

    const connection = await revokeMcpServer("reachable");

    expect(mocks.apiSend).toHaveBeenCalledWith(
      "POST",
      "/api/mcp/servers/reachable/revoke",
    );
    expect(mocks.agentSend).toHaveBeenCalledWith("POST", "/api/mcp/reload");
    expect(connection.status).toBe("approval_required");
  });
});
