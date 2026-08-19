/**
 * DeckEditor — §5d WYSIWYG deck editor: real iframe substrate + measured overlays.
 *
 * Architecture:
 *  - Visual layer: `SlideCanvas` renders `<iframe srcDoc={renderHtml}>` (the real
 *    brand HTML from render_html()) inside a 16:9 container. The iframe shows Fraunces
 *    fonts, accent colours, chrome — an actual SLIDE, not a wireframe.
 *  - Edit layer: `SlideCanvas` overlays transparent `<ElementBox>` boxes measured from
 *    the iframe's `data-element-id` elements (getBoundingClientRect, relative to the
 *    iframe container). Clicking selects; double-clicking opens an editable input.
 *  - Write-back: UNCHANGED from A2. Each ElementBox carries the element's `json_pointer`
 *    (from `GET /deck/editor`). On text commit → PUT /deck/editor patch (via `onPatch`).
 *    After each PUT the parent re-fetches both the LoweredDeck AND the render HTML so
 *    the iframe reflects the edit.
 *
 * `disableDrag` is always true in production (the AuthoredDeck schema has no element
 * geometry — drag would emit patches that always reject). The prop is kept for
 * API compatibility.
 *
 * @module DeckEditor
 */

import { useCallback, useMemo, useState } from "react";
import { LayersPanel } from "./LayersPanel";
import { SlideCanvas } from "./SlideCanvas";
import { SlideThumbnail } from "./SlideThumbnail";
import type { ElementMapEntry } from "./SlideCanvas";
import type { JsonPatchOp, LoweredDeck, LoweredElement } from "./types";

// ─── Props ────────────────────────────────────────────────────────────────────

interface DeckEditorProps {
  /** The lowered deck — source of element_id → json_pointer mapping. */
  deck: LoweredDeck;
  /**
   * The inline HTML string from GET /deck/editor/render — injected as the iframe srcDoc.
   * Null while loading or when the render endpoint is unavailable (degrades gracefully:
   * overlays still exist at zero-rect positions, the canvas shows an empty background).
   */
  renderHtml: string | null;
  /**
   * Called whenever an element is mutated via text-edit.
   * The patch array uses the element's `json_pointer` — e.g.:
   *   [{op:"replace", path:"/slides/0/title", value:"New Title"}]
   */
  onPatch: (patch: JsonPatchOp[]) => void;
  /**
   * Called when the user explicitly selects an element (for agent context injection).
   */
  onElementSelected?: (element: LoweredElement) => void;
  /**
   * A2/§5d: drag is unsupported (the AuthoredDeck schema has no element geometry).
   * The prop is accepted but ignored — drag was never wired in the overlay model.
   */
  disableDrag?: boolean;
}

// ─── DeckEditor ──────────────────────────────────────────────────────────────

export function DeckEditor({
  deck,
  renderHtml,
  onPatch,
  onElementSelected,
}: DeckEditorProps) {
  const [activeSlideIdx, setActiveSlideIdx] = useState(0);
  const [selectedElementId, setSelectedElementId] = useState<string | null>(null);

  const activeSlide = deck.slides[activeSlideIdx] ?? null;

  /**
   * Build a flat element_id → {json_pointer, kind, content} map from ALL slides.
   * SlideCanvas uses this to join the iframe's stamped elements to their json_pointers.
   * Re-computed whenever the deck changes (after a patch round-trip).
   */
  const elementMap = useMemo((): Map<string, ElementMapEntry> => {
    const map = new Map<string, ElementMapEntry>();
    for (const slide of deck.slides) {
      for (const el of slide.elements) {
        map.set(el.element_id, {
          json_pointer: el.json_pointer,
          kind: el.kind,
          content: el.content,
        });
      }
    }
    return map;
  }, [deck]);

  const handleSelectElement = useCallback(
    (elementId: string) => {
      setSelectedElementId(elementId);
      if (onElementSelected) {
        // Look up the element in the active slide first, fall back to all slides.
        const slide = deck.slides[activeSlideIdx];
        const el =
          slide?.elements.find((e) => e.element_id === elementId) ??
          deck.slides.flatMap((s) => s.elements).find((e) => e.element_id === elementId);
        if (el) onElementSelected(el);
      }
    },
    [activeSlideIdx, deck.slides, onElementSelected],
  );

  const handleSelectSlide = useCallback((idx: number) => {
    setActiveSlideIdx(idx);
    setSelectedElementId(null); // clear element selection when switching slides
  }, []);

  // Deselect on background click (outside any overlay ElementBox).
  const handleBackgroundClick = useCallback((e: React.MouseEvent) => {
    const target = e.target as Element;
    if (!target.hasAttribute("data-element-id") && !target.closest("[data-element-id]")) {
      setSelectedElementId(null);
    }
  }, []);

  return (
    <div
      style={{
        display: "flex",
        height: "100%",
        fontFamily: "var(--ui, system-ui, sans-serif)",
      }}
      aria-label="Deck editor"
    >
      {/* ── Slide strip (left) ───────────────────────────────────────────── */}
      <div
        style={{
          width: "120px",
          flexShrink: 0,
          overflowY: "auto",
          borderRight: "1px solid rgba(0,0,0,0.1)",
          background: "#f8f8f8",
          padding: "0.5rem",
          display: "flex",
          flexDirection: "column",
          gap: "0.5rem",
        }}
        aria-label="Slide strip"
      >
        {deck.slides.map((slide, i) => (
          <button
            key={slide.slide_id}
            type="button"
            onClick={() => handleSelectSlide(i)}
            style={{
              position: "relative",
              width: "100%",
              paddingBottom: "56.25%",
              // Neutral light backdrop — the real themed slide is painted on top by
              // SlideThumbnail. (Was `slide.bg_color`: a raw dark swatch that read as
              // a black/blank thumbnail and never matched the main preview.)
              background: "#fff",
              border: i === activeSlideIdx ? "2px solid #6366f1" : "1px solid #ddd",
              borderRadius: "4px",
              cursor: "pointer",
              overflow: "hidden",
              flexShrink: 0,
            }}
            aria-current={i === activeSlideIdx ? "true" : undefined}
            aria-label={`Slide ${i + 1}`}
            title={`Slide ${i + 1}`}
          >
            {/* True mini-render of THIS slide, using the same renderHtml as the main
                canvas — so the rail matches the (themed, light) main preview. */}
            <SlideThumbnail renderHtml={renderHtml} slideId={slide.slide_id} />
            <span
              style={{
                position: "absolute",
                bottom: "2px",
                right: "4px",
                fontSize: "8px",
                color: "#999",
                fontFamily: "var(--ui, system-ui)",
                zIndex: 1,
                pointerEvents: "none",
              }}
            >
              {i + 1}
            </span>
          </button>
        ))}
      </div>

      {/* ── Main canvas area ─────────────────────────────────────────────── */}
      <div
        style={{
          flex: 1,
          overflowY: "auto",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "flex-start",
          padding: "2rem",
          background: "#e8e8e8",
          gap: "1rem",
        }}
        onClick={handleBackgroundClick}
        aria-label="Slide canvas"
      >
        {activeSlide ? (
          <div style={{ width: "100%", maxWidth: "900px" }}>
            <SlideCanvas
              renderHtml={renderHtml}
              activeSlideId={activeSlide.slide_id}
              elementMap={elementMap}
              selectedElementId={selectedElementId}
              onSelectElement={handleSelectElement}
              onPatch={onPatch}
            />
          </div>
        ) : (
          <div
            style={{
              color: "#999",
              fontSize: "0.875rem",
              marginTop: "4rem",
            }}
          >
            No slides in this deck.
          </div>
        )}

        {/* Prev/next navigation */}
        {deck.slides.length > 0 && (
          <div
            style={{
              display: "flex",
              gap: "0.5rem",
              alignItems: "center",
              fontSize: "0.8rem",
              color: "#666",
            }}
          >
            <button
              type="button"
              onClick={() => handleSelectSlide(Math.max(0, activeSlideIdx - 1))}
              disabled={activeSlideIdx === 0}
              className="min-h-11 min-w-11 lg:min-h-0 lg:min-w-0"
              style={{
                padding: "0.25rem 0.75rem",
                border: "1px solid #ccc",
                borderRadius: "4px",
                background: "white",
                cursor: activeSlideIdx === 0 ? "not-allowed" : "pointer",
                opacity: activeSlideIdx === 0 ? 0.4 : 1,
              }}
              aria-label="Previous slide"
            >
              ←
            </button>
            <span>
              {activeSlideIdx + 1} / {deck.slides.length}
            </span>
            <button
              type="button"
              onClick={() =>
                handleSelectSlide(Math.min(deck.slides.length - 1, activeSlideIdx + 1))
              }
              disabled={activeSlideIdx === deck.slides.length - 1}
              className="min-h-11 min-w-11 lg:min-h-0 lg:min-w-0"
              style={{
                padding: "0.25rem 0.75rem",
                border: "1px solid #ccc",
                borderRadius: "4px",
                background: "white",
                cursor:
                  activeSlideIdx === deck.slides.length - 1 ? "not-allowed" : "pointer",
                opacity: activeSlideIdx === deck.slides.length - 1 ? 0.4 : 1,
              }}
              aria-label="Next slide"
            >
              →
            </button>
          </div>
        )}

        {/* Selected element info bar */}
        {selectedElementId && (
          <div
            style={{
              background: "white",
              border: "1px solid #e0e0e0",
              borderRadius: "6px",
              padding: "0.75rem 1rem",
              fontSize: "0.75rem",
              color: "#444",
              maxWidth: "900px",
              width: "100%",
            }}
            aria-live="polite"
            aria-label="Selected element info"
          >
            <strong>Selected:</strong> {selectedElementId}
            {" · "}
            <span style={{ color: "#666" }}>
              {(() => {
                const entry = elementMap.get(selectedElementId);
                if (!entry) return null;
                const editable =
                  entry.kind === "title" ||
                  entry.kind === "subtitle" ||
                  entry.kind === "bullet";
                return editable ? `${entry.kind} — double-click to edit` : entry.kind;
              })()}
            </span>
          </div>
        )}
      </div>

      {/* ── Layers panel (right) ─────────────────────────────────────────── */}
      <LayersPanel
        deck={deck}
        selectedElementId={selectedElementId}
        activeSlideIdx={activeSlideIdx}
        onSelectSlide={handleSelectSlide}
        onSelectElement={handleSelectElement}
      />
    </div>
  );
}
