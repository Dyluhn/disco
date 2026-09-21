/**
 * McpImportBox live tests — the paste-a-config surface.
 *
 * Same discipline as McpSection.live.test.tsx: the REAL data path
 * (component → useImportMcpConfig → api/config.importMcpConfig → apiSend →
 * fetch), with ONLY the network boundary stubbed. Assertions pin the real
 * endpoint + body: the raw pasted text travels verbatim with dry_run, first
 * true (preview) then false (apply) — the client never parses the blob.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { McpImportBox } from "./McpImportBox";
import type { McpImportResult } from "@/types/config";

const { isLiveMock } = vi.hoisted(() => ({ isLiveMock: vi.fn(() => true) }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  isLive: () => isLiveMock(),
}));

const PASTE = '{"mcpServers": {"github": {"command": "npx", "env": {"TOKEN": "abc"}}}}';

function previewResult(created: boolean): McpImportResult {
  return {
    dry_run: !created,
    ok: true,
    servers: [
      {
        name: "github",
        transport: "stdio",
        url: "npx",
        args: [],
        env: { TOKEN: "mcp_github_token" },
        headers: null,
        warnings: ["no server name in the paste; using 'github'"],
        error: null,
        created,
        secrets: [
          {
            ref: "mcp_github_token",
            source_key: "TOKEN",
            value_provided: true,
            already_configured: false,
          },
        ],
      },
    ],
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: "",
    headers: { get: () => null },
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

function renderBox(onClose = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(
    createElement(
      QueryClientProvider,
      { client: qc },
      createElement(McpImportBox, { onClose }),
    ),
  );
  return onClose;
}

describe("McpImportBox", () => {
  beforeEach(() => {
    isLiveMock.mockReturnValue(true);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("previews via POST /api/mcp/servers/import with the raw text and dry_run", async () => {
    const stub = vi.fn<typeof fetch>(async () => jsonResponse(previewResult(false)));
    vi.stubGlobal("fetch", stub);
    renderBox();

    await userEvent.type(screen.getByLabelText("MCP config JSON"), PASTE.replace(/[{[]/g, "$&$&"));
    await userEvent.click(screen.getByRole("button", { name: /Preview/ }));

    const list = await screen.findByRole("list", { name: "Parsed MCP servers" });
    expect(within(list).getByText("github")).toBeInTheDocument();
    const [url, init] = stub.mock.calls[0];
    expect(url).toBe("/api/mcp/servers/import");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual({ text: PASTE, dry_run: true });
    // The preview names the secret ref that will be stored — never the value.
    expect(within(list).getByText("mcp_github_token")).toBeInTheDocument();
    expect(within(list).queryByText(/abc/)).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Add 1 server" }),
    ).toBeEnabled();
  });

  it("applies with dry_run false and closes when servers were created", async () => {
    const stub = vi.fn(async (_url: string, init?: RequestInit) => {
      const body = JSON.parse((init?.body as string) ?? "{}");
      return jsonResponse(previewResult(body.dry_run === false));
    });
    vi.stubGlobal("fetch", stub);
    const onClose = renderBox();

    await userEvent.type(screen.getByLabelText("MCP config JSON"), PASTE.replace(/[{[]/g, "$&$&"));
    await userEvent.click(screen.getByRole("button", { name: /Preview/ }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Add 1 server" })).toBeEnabled(),
    );
    await userEvent.click(screen.getByRole("button", { name: "Add 1 server" }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    const applyCall = stub.mock.calls.find(
      ([, init2]) => JSON.parse((init2?.body as string) ?? "{}").dry_run === false,
    );
    expect(applyCall).toBeDefined();
    expect(JSON.parse(applyCall![1]?.body as string).text).toBe(PASTE);
  });

  it("renders the server's 400 detail for an uninterpretable paste", async () => {
    const stub = vi.fn(async () =>
      jsonResponse({ detail: "not valid JSON: Expecting value at line 1 column 1" }, 400),
    );
    vi.stubGlobal("fetch", stub);
    renderBox();

    await userEvent.type(screen.getByLabelText("MCP config JSON"), "junk");
    await userEvent.click(screen.getByRole("button", { name: /Preview/ }));

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("not valid JSON"),
    );
  });

  it("per-server errors disable the apply button", async () => {
    const result: McpImportResult = {
      dry_run: true,
      ok: false,
      servers: [
        {
          name: "old",
          transport: null,
          url: null,
          args: null,
          env: null,
          headers: null,
          warnings: [],
          error: "SSE transport is not supported",
          created: false,
          secrets: [],
        },
      ],
    };
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(result)));
    renderBox();

    await userEvent.type(screen.getByLabelText("MCP config JSON"), "x");
    await userEvent.click(screen.getByRole("button", { name: /Preview/ }));

    await waitFor(() =>
      expect(screen.getByText("SSE transport is not supported")).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: "Add servers" })).toBeDisabled();
  });
});
