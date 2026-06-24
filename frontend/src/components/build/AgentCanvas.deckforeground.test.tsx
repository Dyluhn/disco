/**
 * BW-15 — auto-foreground the Edit/Export Slides tab when a fresh slides_generate
 * completes. The deck tab must:
 *   - auto-select on the absent→present transition of the editable deck base,
 *   - NOT re-yank a later manual tab choice when the base is unchanged,
 *   - win over the hasScreenshots→Browser auto-switch in the same slides build.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { AgentCanvas } from "@/components/build/AgentCanvas";
import type { AgentEvent } from "@/types/agent";

// BrowserPane uses useLiveBrowserConfig (React Query) — keep it offline.
vi.mock("@/hooks/useModels", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/hooks/useModels")>();
  return {
    ...actual,
    useLiveBrowserConfig: vi.fn(() => ({ data: { enabled: false }, isLoading: false })),
  };
});

// DeckEditorPane fetches the authored deck — stub it so these tab-routing tests
// don't depend on a server.
vi.mock("@/components/build/DeckEditorPane", () => ({
  DeckEditorPane: () => <div data-testid="deck-editor" />,
}));

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const editableDeck = (base: string): AgentEvent =>
  ({
    kind: "observation",
    tool_result: {
      tool_name: "slides_generate",
      success: true,
      structured: { filename: `${base}.html`, base_name: base, editable_source: `${base}.authored.json` },
    },
  }) as unknown as AgentEvent;

const screenshot = (): AgentEvent =>
  ({
    kind: "observation",
    tool_result: { tool_name: "browser", success: true, structured: { screenshot_path: ".pmx/screenshots/0001.png" } },
  }) as unknown as AgentEvent;

// A neutral event that triggers no auto-switch (no screenshot, no deck).
const fileWrite = (path: string): AgentEvent =>
  ({
    id: `w-${path}`,
    kind: "action",
    thought: "",
    tool_call: { tool_name: "file_write", arguments: { path, content: "x" } },
  }) as unknown as AgentEvent;

function selected(name: RegExp): boolean {
  return screen.getByRole("tab", { name }).getAttribute("aria-selected") === "true";
}

describe("AgentCanvas — BW-15 auto-foreground Edit/Export Slides", () => {
  it("foregrounds the deck tab when a fresh slides_generate appears (absent→present)", () => {
    const { rerender } = wrap(<AgentCanvas events={[]} status="RUNNING" cid="c1" />);
    // No deck yet → Browser empty-state is the hero, no deck tab.
    expect(selected(/browser/i)).toBe(true);
    expect(screen.queryByRole("tab", { name: /edit\/export slides/i })).not.toBeInTheDocument();

    // Slides finish → the editable deck appears.
    rerender(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <AgentCanvas events={[editableDeck("deck")]} status="RUNNING" cid="c1" />
      </QueryClientProvider>,
    );
    expect(selected(/edit\/export slides/i)).toBe(true);
  });

  it("does not re-foreground (or fight a manual tab click) on re-render with an unchanged base", async () => {
    const user = userEvent.setup();
    const { rerender } = wrap(<AgentCanvas events={[editableDeck("deck")]} status="RUNNING" cid="c1" />);
    // Auto-foregrounded on mount.
    expect(selected(/edit\/export slides/i)).toBe(true);

    // User deliberately switches to Console.
    await user.click(screen.getByRole("tab", { name: /console/i }));
    expect(selected(/console/i)).toBe(true);

    // A later (neutral) event arrives but the editable base is unchanged → must NOT
    // re-foreground deck and must NOT fight the manual Console choice.
    rerender(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <AgentCanvas events={[editableDeck("deck"), fileWrite("notes.md")]} status="RUNNING" cid="c1" />
      </QueryClientProvider>,
    );
    expect(selected(/console/i)).toBe(true);
    expect(selected(/edit\/export slides/i)).toBe(false);
  });

  it("lets the deck switch win over the browser switch in a slides build (same commit)", () => {
    const { rerender } = wrap(<AgentCanvas events={[]} status="RUNNING" cid="c1" />);
    // Both a screenshot AND a fresh editable deck land at once → deck must win.
    rerender(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <AgentCanvas events={[screenshot(), editableDeck("deck")]} status="RUNNING" cid="c1" />
      </QueryClientProvider>,
    );
    expect(selected(/edit\/export slides/i)).toBe(true);
    expect(selected(/browser/i)).toBe(false);
  });
});
