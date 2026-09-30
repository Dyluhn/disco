import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SkillsSection } from "./SkillsSection";
import { McpSection } from "./McpSection";

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  isLive: () => true,
}));

type SkillRow = {
  id: string;
  name: string;
  description: string;
  body: string;
  enabled: boolean;
};

type McpRow = {
  id: string;
  name: string;
  url: string;
  transport: string;
  status: string;
  enabled: boolean;
};

type RowSpec = { id: string; name: string };

let skillRows: SkillRow[] = [];
let mcpRows: McpRow[] = [];
let clients: QueryClient[] = [];
let blockDeletes = false;
let deleteResolvers: Array<() => void> = [];
let deleteRequests: string[] = [];
let failDeleteIds = new Set<string>();

function reply(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function releaseDeletes() {
  const pending = deleteResolvers.splice(0);
  pending.forEach((resolve) => resolve());
}

beforeEach(() => {
  clients = [];
  skillRows = [];
  mcpRows = [];
  blockDeletes = false;
  deleteResolvers = [];
  deleteRequests = [];
  failDeleteIds = new Set();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = (init?.method ?? "GET").toUpperCase();
      if (method === "GET" && url.startsWith("/api/skills")) return reply([...skillRows]);
      if (method === "GET" && url.startsWith("/api/mcp")) return reply([...mcpRows]);
      if (method === "DELETE") {
        const id = decodeURIComponent(
          String(url).split("?")[0].split("/").filter(Boolean).pop() ?? "",
        );
        deleteRequests.push(id);
        const apply = () => {
          if (failDeleteIds.has(id)) return reply({ detail: "delete failed" }, 500);
          if (String(url).includes("/api/skills")) {
            skillRows = skillRows.filter((s) => s.id !== id);
            return reply({ ok: true });
          }
          if (String(url).includes("/api/mcp")) {
            mcpRows = mcpRows.filter((c) => c.id !== id);
            return reply({ ok: true });
          }
          return reply({ detail: `unknown delete ${url}` }, 404);
        };
        if (blockDeletes) {
          await new Promise<void>((resolve) => {
            deleteResolvers.push(resolve);
          });
          return apply();
        }
        return apply();
      }
      throw new Error(`Unexpected request: ${method} ${url}`);
    }),
  );
});

afterEach(() => {
  cleanup();
  clients.forEach((client) => client.clear());
  vi.unstubAllGlobals();
});

function makeClient() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  clients.push(client);
  return client;
}

async function renderSkills(rows: RowSpec[]) {
  skillRows = rows.map((r) => ({
    id: r.id,
    name: r.name,
    description: "",
    body: "instructions",
    enabled: true,
  }));
  mcpRows = [];
  const user = userEvent.setup();
  const client = makeClient();
  render(
    <QueryClientProvider client={client}>
      <SkillsSection />
      <button>Outside section</button>
    </QueryClientProvider>,
  );
  for (const r of rows) {
    await screen.findByRole("button", { name: `Delete ${r.name}` });
  }
  return { user, client };
}

async function renderMcp(rows: RowSpec[]) {
  skillRows = [];
  mcpRows = rows.map((r) => ({
    id: r.id,
    name: r.name,
    url: `https://${r.id}.example.com/mcp`,
    transport: "stdio",
    status: "connected",
    enabled: true,
  }));
  const user = userEvent.setup();
  const client = makeClient();
  render(
    <QueryClientProvider client={client}>
      <McpSection />
      <button>Outside section</button>
    </QueryClientProvider>,
  );
  for (const r of rows) {
    await screen.findByRole("button", { name: `Remove ${r.name}` });
  }
  return { user, client };
}

async function clickFocused(
  user: ReturnType<typeof userEvent.setup>,
  roleName: string,
) {
  const target = screen.getByRole("button", { name: roleName });
  target.focus();
  expect(target).toHaveFocus();
  await user.click(target);
  return target;
}

const TRIO: RowSpec[] = [
  { id: "alpha-01", name: "Alpha" },
  { id: "bravo-02", name: "Bravo" },
  { id: "charlie-03", name: "Charlie" },
];

describe("delete/remove row focus restoration", () => {
  it("skills: deleting the first row focuses the next row's Delete", async () => {
    const { user } = await renderSkills(TRIO);
    await clickFocused(user, "Delete Alpha");
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Delete Alpha" })).not.toBeInTheDocument(),
    );
    expect(screen.getByText("Bravo")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Delete Bravo" })).toHaveFocus(),
    );
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Delete Bravo" }),
    );
  });

  it("skills: deleting the middle row focuses the next row's Delete", async () => {
    const { user } = await renderSkills(TRIO);
    await clickFocused(user, "Delete Bravo");
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Delete Bravo" })).not.toBeInTheDocument(),
    );
    expect(screen.getByText("Charlie")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Delete Charlie" })).toHaveFocus(),
    );
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Delete Charlie" }),
    );
  });

  it("skills: deleting the last row focuses the previous row's Delete", async () => {
    const { user } = await renderSkills(TRIO);
    await clickFocused(user, "Delete Charlie");
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "Delete Charlie" }),
      ).not.toBeInTheDocument(),
    );
    expect(screen.getByText("Bravo")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Delete Bravo" })).toHaveFocus(),
    );
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Delete Bravo" }),
    );
  });

  it("skills: deleting the only row focuses New skill", async () => {
    const { user } = await renderSkills([{ id: "solo-09", name: "Solo" }]);
    await clickFocused(user, "Delete Solo");
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Delete Solo" })).not.toBeInTheDocument(),
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "New skill" })).toHaveFocus(),
    );
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "New skill" }));
  });

  it("mcp: removing the first row focuses the next row's Remove", async () => {
    const { user } = await renderMcp(TRIO);
    await clickFocused(user, "Remove Alpha");
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Remove Alpha" })).not.toBeInTheDocument(),
    );
    expect(screen.getByText("Bravo")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Remove Bravo" })).toHaveFocus(),
    );
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Remove Bravo" }),
    );
  });

  it("mcp: removing the middle row focuses the next row's Remove", async () => {
    const { user } = await renderMcp(TRIO);
    await clickFocused(user, "Remove Bravo");
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Remove Bravo" })).not.toBeInTheDocument(),
    );
    expect(screen.getByText("Charlie")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Remove Charlie" })).toHaveFocus(),
    );
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Remove Charlie" }),
    );
  });

  it("mcp: removing the last row focuses the previous row's Remove", async () => {
    const { user } = await renderMcp(TRIO);
    await clickFocused(user, "Remove Charlie");
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "Remove Charlie" }),
      ).not.toBeInTheDocument(),
    );
    expect(screen.getByText("Bravo")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Remove Bravo" })).toHaveFocus(),
    );
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Remove Bravo" }),
    );
  });

  it("mcp: removing the only row focuses Add connection", async () => {
    const { user } = await renderMcp([{ id: "solo-09", name: "Solo" }]);
    await clickFocused(user, "Remove Solo");
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Remove Solo" })).not.toBeInTheDocument(),
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Add connection" })).toHaveFocus(),
    );
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Add connection" }),
    );
  });

  it("skills: row and focus stay put while deletion is pending", async () => {
    blockDeletes = true;
    const { user } = await renderSkills(TRIO);
    const target = await clickFocused(user, "Delete Alpha");
    await waitFor(() => expect(deleteRequests).toContain("alpha-01"));
    expect(screen.getByRole("button", { name: "Delete Alpha" })).toBeInTheDocument();
    expect(target).toHaveFocus();
    expect(document.activeElement).toBe(target);
    releaseDeletes();
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Delete Alpha" })).not.toBeInTheDocument(),
    );
  });

  it("mcp: row and focus stay put while removal is pending", async () => {
    blockDeletes = true;
    const { user } = await renderMcp(TRIO);
    const target = await clickFocused(user, "Remove Alpha");
    await waitFor(() => expect(deleteRequests).toContain("alpha-01"));
    expect(screen.getByRole("button", { name: "Remove Alpha" })).toBeInTheDocument();
    expect(target).toHaveFocus();
    expect(document.activeElement).toBe(target);
    releaseDeletes();
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Remove Alpha" })).not.toBeInTheDocument(),
    );
  });

  it("skills: failed deletion keeps the row and its focus", async () => {
    failDeleteIds = new Set(["bravo-02"]);
    const { user } = await renderSkills(TRIO);
    const target = await clickFocused(user, "Delete Bravo");
    await screen.findByRole("alert");
    expect(screen.getByRole("button", { name: "Delete Bravo" })).toBeInTheDocument();
    expect(screen.getByText("Alpha")).toBeInTheDocument();
    expect(screen.getByText("Charlie")).toBeInTheDocument();
    expect(target).toHaveFocus();
    expect(document.activeElement).toBe(target);
  });

  it("mcp: failed removal keeps the row and its focus", async () => {
    failDeleteIds = new Set(["bravo-02"]);
    const { user } = await renderMcp(TRIO);
    const target = await clickFocused(user, "Remove Bravo");
    await screen.findByRole("alert");
    expect(screen.getByRole("button", { name: "Remove Bravo" })).toBeInTheDocument();
    expect(screen.getByText("Alpha")).toBeInTheDocument();
    expect(screen.getByText("Charlie")).toBeInTheDocument();
    expect(target).toHaveFocus();
    expect(document.activeElement).toBe(target);
  });

  it("skills: focus moved elsewhere while pending is not stolen", async () => {
    blockDeletes = true;
    const { user } = await renderSkills(TRIO);
    await clickFocused(user, "Delete Alpha");
    await waitFor(() => expect(deleteRequests).toContain("alpha-01"));
    const outside = screen.getByRole("button", { name: "Outside section" });
    await user.click(outside);
    expect(outside).toHaveFocus();
    releaseDeletes();
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Delete Alpha" })).not.toBeInTheDocument(),
    );
    expect(outside).toHaveFocus();
    expect(document.activeElement).toBe(outside);
  });

  it("mcp: focus moved elsewhere while pending is not stolen", async () => {
    blockDeletes = true;
    const { user } = await renderMcp(TRIO);
    await clickFocused(user, "Remove Alpha");
    await waitFor(() => expect(deleteRequests).toContain("alpha-01"));
    const outside = screen.getByRole("button", { name: "Outside section" });
    await user.click(outside);
    expect(outside).toHaveFocus();
    releaseDeletes();
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Remove Alpha" })).not.toBeInTheDocument(),
    );
    expect(outside).toHaveFocus();
    expect(document.activeElement).toBe(outside);
  });

  it("skills: unrelated source update while pending still removes only the target", async () => {
    blockDeletes = true;
    const { user, client } = await renderSkills(TRIO);
    await clickFocused(user, "Delete Alpha");
    await waitFor(() => expect(deleteRequests).toContain("alpha-01"));
    skillRows = skillRows.map((s) =>
      s.id === "charlie-03" ? { ...s, name: "Charlie Updated" } : s,
    );
    await client.invalidateQueries();
    await screen.findByText("Charlie Updated");
    expect(screen.getByRole("button", { name: "Delete Alpha" })).toBeInTheDocument();
    releaseDeletes();
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Delete Alpha" })).not.toBeInTheDocument(),
    );
    expect(screen.getByText("Bravo")).toBeInTheDocument();
    expect(screen.getByText("Charlie Updated")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Delete Bravo" })).toHaveFocus(),
    );
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Delete Bravo" }),
    );
  });

  it("mcp: unrelated source update while pending still removes only the target", async () => {
    blockDeletes = true;
    const { user, client } = await renderMcp(TRIO);
    await clickFocused(user, "Remove Alpha");
    await waitFor(() => expect(deleteRequests).toContain("alpha-01"));
    mcpRows = mcpRows.map((c) =>
      c.id === "charlie-03" ? { ...c, name: "Charlie Updated" } : c,
    );
    await client.invalidateQueries();
    await screen.findByText("Charlie Updated");
    expect(screen.getByRole("button", { name: "Remove Alpha" })).toBeInTheDocument();
    releaseDeletes();
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Remove Alpha" })).not.toBeInTheDocument(),
    );
    expect(screen.getByText("Bravo")).toBeInTheDocument();
    expect(screen.getByText("Charlie Updated")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Remove Bravo" })).toHaveFocus(),
    );
    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Remove Bravo" }),
    );
  });
});

describe("removal focus ownership", () => {
  const sections = [
    { label: "skills", render: renderSkills, removeName: "Delete Alpha" },
    { label: "mcp", render: renderMcp, removeName: "Remove Alpha" },
  ];

  it.each(sections)("$label: blur to the body while pending relinquishes focus", async (section) => {
    blockDeletes = true;
    const { user } = await section.render(TRIO);
    await clickFocused(user, section.removeName);
    await waitFor(() => expect(deleteRequests).toContain("alpha-01"));
    const prior = document.body.getAttribute("tabindex");
    try {
      document.body.tabIndex = -1;
      document.body.focus();
      expect(document.activeElement).toBe(document.body);
      releaseDeletes();
      await waitFor(() =>
        expect(screen.queryByRole("button", { name: section.removeName })).not.toBeInTheDocument(),
      );
      expect(document.activeElement).toBe(document.body);
    } finally {
      if (prior === null) document.body.removeAttribute("tabindex");
      else document.body.setAttribute("tabindex", prior);
    }
  });

  it.each(sections)("$label: unrelated removal while this request is pending does not restore focus", async (section) => {
    blockDeletes = true;
    const { user, client } = await section.render(TRIO);
    await clickFocused(user, section.removeName);
    await waitFor(() => expect(deleteRequests).toContain("alpha-01"));
    skillRows = skillRows.filter((row) => row.id !== "alpha-01");
    mcpRows = mcpRows.filter((row) => row.id !== "alpha-01");
    await client.invalidateQueries();
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: section.removeName })).not.toBeInTheDocument(),
    );
    expect(document.activeElement).toBe(document.body);
    failDeleteIds.add("alpha-01");
    releaseDeletes();
    await screen.findByRole("alert");
    expect(document.activeElement).toBe(document.body);
  });
});
