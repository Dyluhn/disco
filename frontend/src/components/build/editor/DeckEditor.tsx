/**
 * DeckEditor — §4.5 React deck editor over the positional deck schema.
 *
 * Renders a `LoweredDeck` (the positioned representation produced by
 * `_deck_schema.lower_deck()`) as an interactive canvas of slide elements.
 *
 * Architecture:
 *  - `SlideCanvas` renders one active slide with each `LoweredElement` as an
 *    `ElementBox` (drag/resize/edit-text).
 *  - `LayersPanel` provides a non-spatial element list (walk-up + keyboard nav).
 *  - Mutations (drag / text-edit) emit RFC-6902 JSON Patch arrays via `onPatch`.
 *    The patch path matches the `json_pointer` field on each `LoweredElement` —
 *    the SAME vocabulary the `deck_patch` agent tool uses — so human and agent
 *    edits flow through ONE mutation route.
 *  - The editor stamps `data-element-id` / `data-slide-id` on all rendered
 *    elements so the §4.1 `SelectionOverlay` also works inside the editor
 *    (the overlay substrate is iframe-independent).
 *
 * Acceptance gate: select a deck element → emit a `replace` patch on its
 * `json_pointer` → the caller invokes `deck_patch` → re-rendered deck updates.
 * Visual acceptance (the actual API call + re-render) is the integrator's step.
 *
 * @module DeckEditor
 */

import { useCallback, useState } from "react";
import { LayersPanel } from "./LayersPanel";
import { SlideCanvas } from "./SlideCanvas";
import type { JsonPatchOp, LoweredDeck, LoweredElement } from "./types";

// ─── Props ────────────────────────────────────────────────────────────────────

interface DeckEditorProps {
  /** The lowered deck (geometry computed from the authoring schema). */
  deck: LoweredDeck;
  /**
   * Called whenever an element is mutated via drag or text-edit.
   * The patch array uses the element's `json_pointer` — e.g.:
   *   [{op:"replace", path:"/slides/0/title", value:"New Title"}]
   */
  onPatch: (patch: JsonPatchOp[]) => void;
  /**
   * Called when the user explicitly selects an element (for the agent context
   * injection — the envelope carries `element_id` and `slide_id`).
   */
  onElementSelected?: (element: LoweredElement) => void;
  /**
   * A2: disable the drag affordance entirely (text-edit-only). The AuthoredDeck
   * schema has no element geometry, so a drag would emit a patch that always
   * rejects — rather than silently drop it (a false affordance), the drag handles
   * are removed and only double-click text editing remains.
   */
  disableDrag?: boolean;
}

// ─── DeckEditor ──────────────────────────────────────────────────────────────

export function DeckEditor({ deck, onPatch, onElementSelected, disableDrag = false }: DeckEditorProps) {
  const [activeSlideIdx, setActiveSlideIdx] = useState(0);
  const [selectedElementId, setSelectedElementId] = useState<string | null>(null);

  const activeSlide = deck.slides[activeSlideIdx] ?? null;

  const handleSelectElement = useCallback(
    (elementId: string) => {
      setSelectedElementId(elementId);
      if (onElementSelected) {
        const slide = deck.slides[activeSlideIdx];
        const el = slide?.elements.find((e) => e.element_id === elementId);
        if (el) onElementSelected(el);
      }
    },
    [activeSlideIdx, deck.slides, onElementSelected],
  );

  const handleSelectSlide = useCallback((idx: number) => {
    setActiveSlideIdx(idx);
    setSelectedElementId(null); // clear element selection when switching slides
  }, []);

  // Deselect on canvas background click
  const handleBackgroundClick = useCallback((e: React.MouseEvent) => {
    const target = e.target as Element;
    // Only deselect if the click landed on the canvas itself (not an ElementBox)
    if (!target.hasAttribute("data-element-id")) {
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
              background: slide.bg_color,
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
            {/* Thumbnail label */}
            <span
              style={{
                position: "absolute",
                bottom: "2px",
                right: "4px",
                fontSize: "8px",
                color: "#999",
                fontFamily: "var(--ui, system-ui)",
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
          <div
            style={{ width: "100%", maxWidth: "900px" }}
            data-slide-id={activeSlide.slide_id}
          >
            <SlideCanvas
              slide={activeSlide}
              selectedElementId={selectedElementId}
              onSelectElement={handleSelectElement}
              onPatch={onPatch}
              disableDrag={disableDrag}
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

        {/* Slide counter / prev-next navigation */}
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

        {/* Selected element info */}
        {selectedElementId && activeSlide && (
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
            <strong>Selected:</strong>{" "}
            {selectedElementId}
            {" · "}
            <span style={{ color: "#666" }}>
              {(() => {
                const el = activeSlide.elements.find(
                  (e) => e.element_id === selectedElementId,
                );
                return el
                  ? `${el.kind} — double-click to edit${disableDrag ? "" : ", drag to move"}`
                  : null;
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
