import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AgentCanvas } from "@/components/build/AgentCanvas";
import { DeckEditorPane } from "@/components/build/DeckEditorPane";
import { DeckExportBar } from "@/components/build/DeckExportBar";
import { ImageGenSection } from "@/components/settings/ImageGenSection";
import { ElementBox } from "@/components/build/editor/ElementBox";
import type { AgentEvent } from "@/types/agent";
import type { ImageGenConfig, OpenRouterKeyStatus, OpenRouterModel } from "@/types/models";

const hookState = vi.hoisted(() => ({
  imageGen: {
    provider: "openrouter",
    base_url: "",
    api_key_env: "",
    model: "",
  } as ImageGenConfig,
  openRouterKey: { configured: false, locked: false, can_store: true } as OpenRouterKeyStatus,
  openRouterModels: [] as OpenRouterModel[],
}));

vi.mock("@/hooks/useModels", () => ({
  useLiveBrowserConfig: () => ({ data: { enabled: false } }),
  useImageGenConfig: () => ({ data: hookState.imageGen, isLoading: false }),
  useUpdateImageGenConfig: () => ({ mutate: vi.fn(), isPending: false }),
  useOpenRouterKey: () => ({ data: hookState.openRouterKey }),
  useOpenRouterModels: () => ({ data: hookState.openRouterModels }),
}));

vi.mock("@/api/agent", async () => {
  const actual = await vi.importActual<typeof import("@/api/agent")>("@/api/agent");
  return {
    ...actual,
    getDeckForEditor: vi.fn(async () => ({
      title: "Editable deck",
      theme_name: "disco",
      theme_mode: "light",
      slides: [],
    })),
    getDeckRenderHtml: vi.fn(async () => "<section data-slide-id='slide-0'></section>"),
    patchDeck: vi.fn(),
  };
});

vi.mock("@/components/build/editor/DeckEditor", () => ({
  DeckEditor: () => <div data-testid="deck-editor-body">Deck editor body</div>,
}));

describe("AuthorB unbiased gate — W-13/W-16/W-19/W-21 slide UI", () => {
  beforeEach(() => {
    hookState.imageGen = {
      provider: "openrouter",
      base_url: "",
      api_key_env: "",
      model: "",
    };
    hookState.openRouterKey = { configured: false, locked: false, can_store: true };
    hookState.openRouterModels = [];
  });

  it("W-16/W-19 shared deck export bar offers prominent Theme plus pptx/html and no premature PDF link", () => {
    render(<DeckExportBar conversationId="conv_deck" base="market-brief" title="Market brief" />);

    expect(screen.getByText("Theme")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /PowerPoint/i })).toHaveAttribute(
      "data-export-fmt",
      "pptx",
    );
    expect(screen.getByRole("link", { name: /Web page/i })).toHaveAttribute(
      "data-export-fmt",
      "html",
    );
    expect(screen.queryByRole("link", { name: /PDF/i })).not.toBeInTheDocument();
  });

  it("W-19 editor pane renders the export bar on the editor itself", async () => {
    render(<DeckEditorPane cid="conv_deck" base="market-brief" />);

    await waitFor(() => expect(screen.getByTestId("deck-editor-body")).toBeInTheDocument());
    expect(screen.getByText("Theme")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /PowerPoint/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Web page/i })).toBeInTheDocument();
  });

  it("W-21 renames the editable deck tab to Edit/Export Slides", async () => {
    const events: AgentEvent[] = [
      {
        id: "slides-action",
        kind: "action",
        thought: "Generate slides",
        tool_call: { tool_name: "slides_generate", arguments: { filename: "market-brief" } },
        source: "agent",
      },
      {
        id: "slides-obs",
        kind: "observation",
        action_id: "slides-action",
        tool_result: {
          tool_name: "slides_generate",
          success: true,
          content: "slides",
          structured: {
            filename: "market-brief.pptx",
            base_name: "market-brief",
            format: "pptx",
            slide_count: 4,
            editable_source: "market-brief.authored.json",
          },
        },
        source: "environment",
      },
    ];

    render(<AgentCanvas events={events} status="FINISHED" cid="conv_deck" />);

    expect(screen.getByRole("tab", { name: /Edit\/Export Slides/i })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /^Edit Slides$/i })).not.toBeInTheDocument();
    // The foreground editor starts its async deck load. Keep it mounted through
    // that public commit so no DeckEditorPane state update outlives the test.
    expect(await screen.findByTestId("deck-editor-body")).toBeInTheDocument();
  });

  it("W-21 edit-text input uses dark text against the highlighted edit background", async () => {
    const user = userEvent.setup();
    render(
      <ElementBox
        elementId="slide-0:title"
        jsonPointer="/slides/0/title"
        kind="title"
        content="Editable title"
        rect={{ left: 0, top: 0, width: 220, height: 60 }}
        selected
        onSelect={() => {}}
        onPatch={() => {}}
      />,
    );

    await user.dblClick(screen.getByRole("button", { name: /title: Editable title/i }));
    const input = screen.getByLabelText("Edit title");
    expect(input).toHaveStyle({ color: "#1a1813" });
  });

  it("W-50 settings no longer offers bundled/procedural image generation", () => {
    render(<ImageGenSection />);

    expect(screen.queryByText(/procedural/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/bundled/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /OpenRouter/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /ComfyUI/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /OpenAI-compatible/i })).toBeInTheDocument();
  });
});
