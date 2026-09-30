import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Skill } from "@/types/config";
import { SkillsSection } from "./SkillsSection";
import { McpSection } from "./McpSection";

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  isLive: () => true,
}));

const variants = [
  { kind: "new", opener: "New skill", field: "Skill name", save: "Save skill" },
  { kind: "edit", opener: "Edit Existing", field: "Skill name", save: "Save skill" },
  { kind: "connection", opener: "Add connection", field: "MCP server name", save: "Add connection" },
  { kind: "import", opener: "Paste config", field: "MCP config JSON", save: "Add 1 server" },
] as const;
type Variant = (typeof variants)[number];

let skills: Skill[];
let clients: QueryClient[];

function reply(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  clients = [];
  skills = [
    { id: "stable-id", name: "Existing", description: "", body: "Existing instructions", enabled: true },
  ];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url === "/api/skills" && method === "GET") return reply(skills);
      if (url === "/api/mcp" && method === "GET") return reply([]);
      const body = JSON.parse(String(init?.body ?? "{}"));
      if (url === "/api/skills" && method === "POST") {
        const skill = { ...body, id: "new-id", enabled: true };
        skills = [...skills, skill];
        return reply(skill);
      }
      if (url === "/api/skills/stable-id" && method === "PUT") {
        skills = [{ ...skills[0], ...body }];
        return reply(skills[0]);
      }
      if (url === "/api/mcp/servers" && method === "POST") {
        return reply({ ...body, id: body.name, status: "disconnected" });
      }
      if (url === "/api/mcp/servers/import" && method === "POST") {
        return reply({
          dry_run: body.dry_run,
          ok: true,
          servers: [
            {
              name: "example",
              transport: "stdio",
              url: "example",
              args: [],
              env: {},
              headers: null,
              warnings: [],
              error: null,
              created: !body.dry_run,
              secrets: [],
            },
          ],
        });
      }
      throw new Error("Unexpected request: " + method + " " + url);
    }),
  );
});

afterEach(() => {
  cleanup();
  clients.forEach((client) => client.clear());
  vi.unstubAllGlobals();
});

async function open(variant: Variant) {
  const user = userEvent.setup();
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  clients.push(client);
  render(
    <QueryClientProvider client={client}>
      {variant.kind === "new" || variant.kind === "edit" ? <SkillsSection /> : <McpSection />}
      <button>Outside section</button>
    </QueryClientProvider>,
  );
  const opener = await screen.findByRole("button", { name: variant.opener });
  await waitFor(() => expect(opener).toBeEnabled());
  expect(opener).not.toHaveFocus();
  opener.focus();
  await user.keyboard("{Enter}");
  const field = screen.getByRole("textbox", { name: variant.field });
  expect(field).toHaveFocus();
  return { user, field, opener };
}

async function fill(variant: Variant, user: ReturnType<typeof userEvent.setup>) {
  if (variant.kind === "new" || variant.kind === "edit") {
    await user.clear(screen.getByRole("textbox", { name: "Skill name" }));
    await user.type(screen.getByRole("textbox", { name: "Skill name" }), "Renamed");
    if (variant.kind === "new") {
      await user.type(
        screen.getByRole("textbox", { name: "Skill instructions (markdown)" }),
        "Retain this draft.",
      );
    }
  } else if (variant.kind === "connection") {
    await user.type(screen.getByRole("textbox", { name: "MCP server name" }), "example");
    await user.type(screen.getByRole("textbox", { name: "MCP server URL" }), "https://example.com/mcp");
  } else {
    await user.click(screen.getByRole("textbox", { name: "MCP config JSON" }));
    await user.paste('{"mcpServers":{"example":{"command":"example"}}}');
    await user.click(screen.getByRole("button", { name: "Preview" }));
    await screen.findByRole("list", { name: "Parsed MCP servers" });
  }
}

describe("Inline settings editor keyboard focus", () => {
  it.each(variants)("opens $kind at the first field and Cancel restores the opener", async (variant) => {
    const { user, field } = await open(variant);
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    const replacement = screen.getByRole("button", { name: variant.opener });
    expect(replacement).toHaveFocus();
    expect(field).not.toBeInTheDocument();
  });

  it.each(variants)("successful $kind save restores its own opener", async (variant) => {
    const { user } = await open(variant);
    await fill(variant, user);
    await user.click(screen.getByRole("button", { name: variant.save }));
    const name = variant.kind === "edit" ? "Edit Renamed" : variant.opener;
    await waitFor(() => expect(screen.getByRole("button", { name })).toHaveFocus());
    expect(screen.queryByRole("textbox", { name: variant.field })).not.toBeInTheDocument();
  });
});
