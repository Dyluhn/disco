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
let rejectSave: boolean;
let clients: QueryClient[];
let holdSave: Promise<void> | undefined;
let writes: number;
let holdPreview: Promise<void> | undefined;
let previews: number;

function reply(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status, headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  clients = [];
  skills = [{ id: "stable-id", name: "Existing", description: "", body: "Existing instructions", enabled: true }];
  rejectSave = false;
  holdSave = undefined;
  writes = 0;
  holdPreview = undefined;
  previews = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url === "/api/skills" && method === "GET") return reply(skills);
    if (url === "/api/mcp" && method === "GET") return reply([]);
    const body = JSON.parse(String(init?.body ?? "{}"));
    if (method !== "GET" && !body.dry_run) { writes += 1; await holdSave; }
    if (body.dry_run) { previews += 1; await holdPreview; }
    if (rejectSave) return reply({ detail: "Save rejected" }, 422);
    if (url === "/api/skills" && method === "POST") {
      const skill = { ...body, id: "new-id", enabled: true };
      skills = [...skills, skill];
      return reply(skill);
    }
    if (url === "/api/skills/stable-id" && method === "PUT") {
      skills = [{ ...skills[0], ...body }];
      return reply(skills[0]);
    }
    if (url === "/api/mcp/servers" && method === "POST") return reply({ ...body, id: body.name, status: "disconnected" });
    if (url === "/api/mcp/servers/import" && method === "POST") {
      return reply({
        dry_run: body.dry_run, ok: true,
        servers: [{ name: "example", transport: "stdio", url: "example",
          args: [], env: {}, headers: null, warnings: [], error: null,
          created: !body.dry_run, secrets: [] }],
      });
    }
    throw new Error("Unexpected request: " + method + " " + url);
  }));
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

  it.each(variants)("opens $kind at the first field and Cancel restores the remounted opener", async (variant) => {
    const { user, field, opener } = await open(variant);
    await user.type(field, " draft");
    await user.tab();
    expect(field).not.toHaveFocus();
    await user.tab({ shift: true });
    expect(field).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(field).toBeInTheDocument();
    screen.getByRole("button", { name: "Outside section" }).focus();
    await user.tab({ shift: true });
    expect(screen.getByRole("button", { name: "Outside section" })).not.toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: "Outside section" })).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    const replacement = screen.getByRole("button", { name: variant.opener });
    expect(replacement).toHaveFocus();
    expect(replacement).not.toBe(opener);
  });

  it.each(variants)("successful $kind save restores its own opener, including a renamed skill", async (variant) => {
    const { user } = await open(variant);
    await fill(variant, user);
    await user.click(screen.getByRole("button", { name: variant.save }));
    const name = variant.kind === "edit" ? "Edit Renamed" : variant.opener;
    await waitFor(() => expect(screen.getByRole("button", { name })).toHaveFocus());
    expect(screen.queryByRole("textbox", { name: variant.field })).not.toBeInTheDocument();
  });

  it.each(variants)("pending $kind save stays focusable and suppresses duplicate activation", async (variant) => {
    const { user, field } = await open(variant);
    await fill(variant, user);
    let release!: () => void;
    holdSave = new Promise<void>((resolve) => { release = resolve; });
    const save = screen.getByRole("button", { name: variant.save });
    await user.click(save);
    await waitFor(() => expect(writes).toBe(1));
    expect(save).toHaveAttribute("aria-disabled", "true");
    // Native disabled buttons lose focus in Chromium; jsdom alone hides that bug.
    expect((save as HTMLButtonElement).disabled).toBe(false);
    expect(save).toHaveFocus();
    await user.click(save);
    await user.keyboard("{Enter}");
    expect(writes).toBe(1);
    rejectSave = true;
    release();
    await screen.findByRole("alert");
    await waitFor(() => expect(save).toHaveAttribute("aria-disabled", "false"));
    expect(field).toBeInTheDocument();
    expect(save).toHaveFocus();
  });

  it.each([false, true])("import preview retains focus after success or rejection (%s)", async (reject) => {
    const { user, field } = await open(variants[3]);
    await user.type(field, "draft");
    let release!: () => void;
    holdPreview = new Promise<void>((resolve) => { release = resolve; });
    const preview = screen.getByRole("button", { name: "Preview" });
    await user.click(preview);
    await waitFor(() => expect(previews).toBe(1));
    expect(preview).toHaveAttribute("aria-disabled", "true");
    expect((preview as HTMLButtonElement).disabled).toBe(false);
    expect(preview).toHaveFocus();
    await user.click(preview);
    await user.keyboard("{Enter}");
    expect(previews).toBe(1);
    rejectSave = reject;
    release();
    if (reject) await screen.findByRole("alert");
    else await screen.findByRole("list", { name: "Parsed MCP servers" });
    await waitFor(() => expect(preview).toHaveAttribute("aria-disabled", "false"));
    expect(preview).toHaveFocus();
    expect(field).toHaveValue("draft");
  });

  it.each(variants)("failed $kind save retains the editor, draft and keyboard focus", async (variant) => {
    const { user, field } = await open(variant);
    await fill(variant, user);
    const draft = (field as HTMLInputElement | HTMLTextAreaElement).value;
    rejectSave = true;
    const save = screen.getByRole("button", { name: variant.save });
    await user.click(save);
    await screen.findByRole("alert");
    await waitFor(() => expect(save).toBeEnabled());
    expect(field).toHaveValue(draft);
    expect(save).toHaveFocus();
    await user.click(field);
    await user.keyboard("{End}X");
    expect(field).toHaveValue(draft + "X");
    expect(field).toHaveFocus();
  });
});
