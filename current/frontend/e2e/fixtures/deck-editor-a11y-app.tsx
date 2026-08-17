import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { DeckEditor } from "@/components/build/editor/DeckEditor";
import type {
  JsonPatchOp,
  LoweredDeck,
  LoweredSlide,
} from "@/components/build/editor/types";

function slideFor(title: string, index: number): LoweredSlide {
  const slideId = `slide-${index}`;
  return {
    slide_id: slideId,
    slide_idx: index,
    layout: "title",
    bg_color: "#ffffff",
    elements: [
      {
        element_id: `${slideId}:title`,
        slide_id: slideId,
        kind: "title",
        content: title,
        geometry: { x: 4, y: 6, w: 92, h: 12 },
        font_size_vw: 2.5,
        font_weight: "bold",
        font_style: "normal",
        json_pointer: `/slides/${index}/title`,
      },
    ],
  };
}

function deckFor(titles: string[]): LoweredDeck {
  return {
    title: "Accessibility regression deck",
    theme_name: "disco",
    theme_mode: "light",
    slides: titles.map(slideFor),
  };
}

function escapeHtml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function renderHtmlFor(deck: LoweredDeck): string {
  const slides = deck.slides
    .map((slide, index) => {
      const title = slide.elements.find((element) => element.kind === "title")?.content ?? "";
      return `<section class="slide${index === 0 ? " active" : ""}" data-slide-id="${slide.slide_id}"><h2 data-element-id="${slide.slide_id}:title" data-slide-id="${slide.slide_id}">${escapeHtml(title)}</h2></section>`;
    })
    .join("");
  return `<!doctype html><html lang="en"><head><style>.slide{display:none}.slide.active{display:flex}</style></head><body><div class="deck">${slides}</div></body></html>`;
}

function titleList(deck: LoweredDeck): string[] {
  return deck.slides.map(
    (slide) => slide.elements.find((element) => element.kind === "title")?.content ?? "",
  );
}

const STORAGE_KEY = "disco.e2e.deck-editor-a11y";

function initialDeck(): LoweredDeck {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored) {
    try {
      return JSON.parse(stored) as LoweredDeck;
    } catch {
      localStorage.removeItem(STORAGE_KEY);
    }
  }
  return deckFor(["Original title", "Second title"]);
}

export function DeckEditorA11yApp() {
  const [history, setHistory] = useState<LoweredDeck[]>(() => [initialDeck()]);
  const [historyIndex, setHistoryIndex] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const layoutLabelRef = useRef<HTMLOutputElement>(null);
  const deck = history[historyIndex];
  const renderHtml = useMemo(() => renderHtmlFor(deck), [deck]);

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(deck));
  }, [deck]);

  // Browser oracle: capture the accessibility DOM in the same layout commit as a
  // server-authoritative deck transition. A later passive measurement update must not
  // be required to replace an obsolete accessible name.
  useLayoutEffect(() => {
    const label = rootRef.current
      ?.querySelector('[data-disco-control="build.deck-element"]')
      ?.getAttribute("aria-label");
    if (layoutLabelRef.current) layoutLabelRef.current.value = label ?? "missing";
  }, [deck]);

  const commit = useCallback(
    (next: LoweredDeck) => {
      setHistory((current) => [...current.slice(0, historyIndex + 1), next]);
      setHistoryIndex(historyIndex + 1);
    },
    [historyIndex],
  );

  const onPatch = useCallback(
    (patch: JsonPatchOp[]) => {
      const titles = titleList(deck);
      for (const operation of patch) {
        const match =
          operation.op === "replace" && operation.path.match(/^\/slides\/(\d+)\/title$/);
        if (!match || typeof operation.value !== "string") continue;
        titles[Number(match[1])] = operation.value;
      }
      commit(deckFor(titles));
    },
    [commit, deck],
  );

  const titles = titleList(deck);
  return (
    <main style={{ display: "flex", height: "100%", flexDirection: "column" }}>
      <div role="toolbar" aria-label="Deck history operations">
        <button
          type="button"
          disabled={historyIndex === 0}
          onClick={() => setHistoryIndex((index) => Math.max(0, index - 1))}
        >
          Undo
        </button>
        <button
          type="button"
          disabled={historyIndex === history.length - 1}
          onClick={() => setHistoryIndex((index) => Math.min(history.length - 1, index + 1))}
        >
          Redo
        </button>
        <button type="button" onClick={() => commit(deckFor([...titles].reverse()))}>
          Reorder slides
        </button>
        <button
          type="button"
          onClick={() => commit(deckFor([titles[0], `${titles[0]} copy`, ...titles.slice(1)]))}
        >
          Duplicate current slide
        </button>
        <output ref={layoutLabelRef} aria-label="Layout accessible name" />
      </div>
      <div ref={rootRef} style={{ minHeight: 0, flex: 1 }}>
        <DeckEditor
          deck={deck}
          renderHtml={renderHtml}
          onPatch={onPatch}
        />
      </div>
    </main>
  );
}
