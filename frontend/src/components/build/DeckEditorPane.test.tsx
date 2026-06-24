/**
 * DeckEditorPane — A2 host tests.
 *
 * Covers:
 *  1. Renders the editor from a stubbed GET (getDeckForEditor).
 *  2. A text edit fires patchDeck (PUT) and the pane reflects the server-returned
 *     lowered deck (server is the source of truth).
 *  3. A 409 (suspended sandbox) surfaces the clear "re-open this build" notice.
 *  4. A load failure surfaces an error, not a silent blank.
 */

import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { ApiError } from "@/api/client";
import type { LoweredDeck } from "@/components/build/editor/types";
import { DeckEditorPane } from "./DeckEditorPane";

vi.mock("@/api/agent", () => ({
  getDeckForEditor: vi.fn(),
  patchDeck: vi.fn(),
  // getDeckRenderHtml: return empty string so the iframe renders (non-fatal if it fails).
  getDeckRenderHtml: vi.fn().mockResolvedValue(""),
}));

// BW-14: a second template so the lifted Theme picker can switch.
vi.mock("@/hooks/useTemplates", () => ({
  useTemplates: () => [
    { id: "disco-light", name: "disco", mode: "light", label: "Disco", description: "Default", accent: "#4077a3", bg: "#fcfcfa", default: true },
    { id: "midnight-dark", name: "midnight", mode: "dark", label: "Midnight", description: "Dark", accent: "#d9a441", bg: "#0d1017", default: false },
  ],
}));

import { getDeckForEditor, getDeckRenderHtml, patchDeck } from "@/api/agent";

const getMock = getDeckForEditor as ReturnType<typeof vi.fn>;
const putMock = patchDeck as ReturnType<typeof vi.fn>;
const renderMock = getDeckRenderHtml as ReturnType<typeof vi.fn>;

function makeDeck(title: string): LoweredDeck {
  return {
    title: "Deck",
    theme_name: "disco",
    theme_mode: "light",
    slides: [
      {
        slide_id: "slide-0",
        slide_idx: 0,
        layout: "title",
        bg_color: "#ffffff",
        elements: [
          {
            element_id: "slide-0:title",
            slide_id: "slide-0",
            kind: "title",
            content: title,
            geometry: { x: 4, y: 6, w: 92, h: 12 },
            font_size_vw: 2.5,
            font_weight: "bold",
            font_style: "normal",
            json_pointer: "/slides/0/title",
          },
        ],
      },
    ],
  };
}

describe("DeckEditorPane", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders the editor from the stubbed GET", async () => {
    getMock.mockResolvedValue(makeDeck("Original Title"));
    render(<DeckEditorPane cid="conv_1" base="deck" />);
    expect(await screen.findAllByText("Original Title")).not.toHaveLength(0);
    expect(getMock).toHaveBeenCalledWith("conv_1", "deck");
  });

  it("fires patchDeck on edit and reflects the server-returned lowered deck", async () => {
    getMock.mockResolvedValue(makeDeck("Original Title"));
    putMock.mockResolvedValue({
      ok: true,
      lowered: makeDeck("Server Title"),
      html_file: "deck.html",
      pptx_file: "deck.pptx",
    });
    const { container } = render(<DeckEditorPane cid="conv_1" base="deck" />);
    // Wait for the editor to mount.
    await screen.findAllByText("Original Title");

    const titleBox = container.querySelector('[data-element-id="slide-0:title"]')!;
    fireEvent.doubleClick(titleBox);
    const input = container.querySelector('input[type="text"]')!;
    fireEvent.change(input, { target: { value: "Edited" } });
    fireEvent.blur(input);

    await waitFor(() => expect(putMock).toHaveBeenCalledTimes(1));
    // The PUT carried the replace patch for the title pointer.
    const [, base, patch] = putMock.mock.calls[0];
    expect(base).toBe("deck");
    expect(patch).toEqual([
      { op: "replace", path: "/slides/0/title", value: "Edited" },
    ]);
    // The pane now shows the SERVER's title, not the optimistic local edit.
    expect(await screen.findAllByText("Server Title")).not.toHaveLength(0);
  });

  it("warns that the PDF is stale after an edit when the server flags it", async () => {
    getMock.mockResolvedValue(makeDeck("Original Title"));
    putMock.mockResolvedValue({
      ok: true,
      lowered: makeDeck("Server Title"),
      html_file: "deck.html",
      pptx_file: "deck.pptx",
      pdf_stale: true,
    });
    const { container } = render(<DeckEditorPane cid="conv_1" base="deck" />);
    await screen.findAllByText("Original Title");

    const titleBox = container.querySelector('[data-element-id="slide-0:title"]')!;
    fireEvent.doubleClick(titleBox);
    const input = container.querySelector('input[type="text"]')!;
    fireEvent.change(input, { target: { value: "Edited" } });
    fireEvent.blur(input);

    expect(
      await screen.findByText(/PDF download is now out of date/i),
    ).toBeInTheDocument();
  });

  it("surfaces the re-open notice on a 409 (suspended sandbox)", async () => {
    getMock.mockResolvedValue(makeDeck("Original Title"));
    putMock.mockRejectedValue(new ApiError("no_live_sandbox", 409));
    const { container } = render(<DeckEditorPane cid="conv_1" base="deck" />);
    await screen.findAllByText("Original Title");

    const titleBox = container.querySelector('[data-element-id="slide-0:title"]')!;
    fireEvent.doubleClick(titleBox);
    const input = container.querySelector('input[type="text"]')!;
    fireEvent.change(input, { target: { value: "Edited" } });
    fireEvent.blur(input);

    await waitFor(() => expect(putMock).toHaveBeenCalled());
    expect(
      await screen.findByText(/re-open this build to edit slides/i),
    ).toBeInTheDocument();
  });

  it("serializes saves — a second edit during an in-flight save is dropped, not raced", async () => {
    getMock.mockResolvedValue(makeDeck("Original Title"));
    // A PUT that never resolves during the test → the save stays in flight.
    let resolvePut: (v: unknown) => void = () => {};
    putMock.mockReturnValue(
      new Promise((res) => {
        resolvePut = res;
      }),
    );
    const { container } = render(<DeckEditorPane cid="conv_1" base="deck" />);
    await screen.findAllByText("Original Title");

    const titleBox = container.querySelector('[data-element-id="slide-0:title"]')!;
    // First edit → fires the (pending) PUT.
    fireEvent.doubleClick(titleBox);
    fireEvent.change(container.querySelector('input[type="text"]')!, {
      target: { value: "Edit 1" },
    });
    fireEvent.blur(container.querySelector('input[type="text"]')!);
    await waitFor(() => expect(putMock).toHaveBeenCalledTimes(1));

    // Second edit WHILE the first save is in flight → must be dropped (no 2nd PUT).
    fireEvent.doubleClick(titleBox);
    const second = container.querySelector('input[type="text"]');
    if (second) {
      fireEvent.change(second, { target: { value: "Edit 2" } });
      fireEvent.blur(second);
    }
    await Promise.resolve();
    expect(putMock).toHaveBeenCalledTimes(1); // still only the first save

    resolvePut({
      ok: true,
      lowered: makeDeck("Edit 1"),
      html_file: "deck.html",
      pptx_file: "deck.pptx",
    });
  });

  it("surfaces a load error rather than a blank pane", async () => {
    getMock.mockRejectedValue(new ApiError("not found", 404));
    render(<DeckEditorPane cid="conv_1" base="deck" />);
    expect(await screen.findByText(/not found/i)).toBeInTheDocument();
  });

  it("BW-14: switching the export Theme re-renders the LIVE preview (re-fetches with the new template)", async () => {
    getMock.mockResolvedValue(makeDeck("Original Title"));
    render(<DeckEditorPane cid="conv_1" base="deck" />);
    await screen.findAllByText("Original Title");

    // The initial preview render used the default template…
    await waitFor(() =>
      expect(renderMock).toHaveBeenCalledWith("conv_1", "deck", "disco-light"),
    );

    // …and changing the lifted Theme re-fetches the render with the new template,
    // so the slides (not just the export hrefs) visibly re-render.
    const select = screen.getByLabelText(/Theme/i) as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "midnight-dark" } });

    await waitFor(() =>
      expect(renderMock).toHaveBeenCalledWith("conv_1", "deck", "midnight-dark"),
    );
  });
});
