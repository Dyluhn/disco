/**
 * LayersPanel — non-spatial element hierarchy walk-up list (§4.5).
 *
 * Renders the `slides[].elements[]` tree as a nested list. Clicking an entry
 * selects the corresponding element (same event as clicking it in the canvas).
 * Provides a non-pointer-dependent way to navigate the deck structure — useful
 * when elements overlap or are too small to click on the canvas.
 *
 * @module LayersPanel
 */

import type { LoweredDeck } from "./types";

interface LayersPanelProps {
  deck: LoweredDeck;
  /** The currently selected element_id (may be null). */
  selectedElementId: string | null;
  /** The currently active slide index. */
  activeSlideIdx: number;
  /** Called when a slide is clicked (switches to that slide). */
  onSelectSlide: (slideIdx: number) => void;
  /** Called when an element is clicked (selects that element). */
  onSelectElement: (elementId: string) => void;
}

// ─── Kind icon / label ────────────────────────────────────────────────────────

const KIND_LABEL: Record<string, string> = {
  title: "T",
  subtitle: "St",
  bullet: "•",
  chart: "Ch",
  table: "Tb",
  image_prompt: "Img",
  notes: "N",
};

function KindBadge({ kind }: { kind: string }) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        width: "1.5em",
        height: "1.5em",
        marginRight: "0.4em",
        borderRadius: "3px",
        background: "rgba(99,102,241,0.15)",
        color: "#6366f1",
        fontSize: "0.7em",
        fontWeight: "bold",
        flexShrink: 0,
      }}
      aria-hidden
    >
      {KIND_LABEL[kind] ?? "?"}
    </span>
  );
}

// ─── LayersPanel ──────────────────────────────────────────────────────────────

export function LayersPanel({
  deck,
  selectedElementId,
  activeSlideIdx,
  onSelectSlide,
  onSelectElement,
}: LayersPanelProps) {
  return (
    <aside
      aria-label="Layers"
      style={{
        width: "200px",
        flexShrink: 0,
        borderLeft: "1px solid rgba(0,0,0,0.1)",
        overflowY: "auto",
        fontFamily: "var(--ui, system-ui, sans-serif)",
        fontSize: "0.75rem",
      }}
    >
      <div
        style={{
          padding: "0.5rem 0.75rem",
          fontWeight: "bold",
          borderBottom: "1px solid rgba(0,0,0,0.1)",
          color: "#555",
          fontSize: "0.7rem",
          textTransform: "uppercase",
          letterSpacing: "0.08em",
        }}
      >
        Layers
      </div>

      <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
        {deck.slides.map((slide, i) => {
          const isActiveSlide = i === activeSlideIdx;
          return (
            <li key={slide.slide_id}>
              {/* Slide row */}
              <button
                type="button"
                onClick={() => onSelectSlide(i)}
                className="min-h-11 lg:min-h-0"
                style={{
                  display: "flex",
                  alignItems: "center",
                  width: "100%",
                  padding: "0.4rem 0.75rem",
                  background: isActiveSlide ? "rgba(99,102,241,0.1)" : "transparent",
                  border: "none",
                  borderBottom: "1px solid rgba(0,0,0,0.05)",
                  cursor: "pointer",
                  textAlign: "left",
                  fontWeight: isActiveSlide ? "bold" : "normal",
                  color: isActiveSlide ? "#4f46e5" : "#333",
                  fontSize: "0.73rem",
                }}
                aria-current={isActiveSlide ? "true" : undefined}
              >
                <span
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    justifyContent: "center",
                    width: "1.5em",
                    height: "1.5em",
                    marginRight: "0.4em",
                    borderRadius: "3px",
                    background: isActiveSlide ? "#6366f1" : "#ddd",
                    color: isActiveSlide ? "white" : "#555",
                    fontSize: "0.65em",
                    flexShrink: 0,
                  }}
                >
                  {i + 1}
                </span>
                <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {slide.elements.find((e) => e.kind === "title")?.content ?? `Slide ${i + 1}`}
                </span>
              </button>

              {/* Element rows (only shown when this slide is active) */}
              {isActiveSlide && (
                <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
                  {slide.elements.map((el) => {
                    const isSelected = el.element_id === selectedElementId;
                    return (
                      <li key={el.element_id}>
                        <button
                          type="button"
                          onClick={() => onSelectElement(el.element_id)}
                          data-element-id={el.element_id}
                          data-slide-id={el.slide_id}
                          data-disco-control="build.deck-layer"
                          className="min-h-11 lg:min-h-0"
                          style={{
                            display: "flex",
                            alignItems: "center",
                            width: "100%",
                            padding: "0.3rem 0.75rem 0.3rem 1.75rem",
                            background: isSelected ? "rgba(99,102,241,0.18)" : "transparent",
                            border: "none",
                            borderBottom: "1px solid rgba(0,0,0,0.03)",
                            cursor: "pointer",
                            textAlign: "left",
                            color: isSelected ? "#4f46e5" : "#444",
                            fontSize: "0.7rem",
                          }}
                          aria-pressed={isSelected}
                          title={el.content}
                        >
                          <KindBadge kind={el.kind} />
                          <span
                            style={{
                              overflow: "hidden",
                              textOverflow: "ellipsis",
                              whiteSpace: "nowrap",
                              flex: 1,
                            }}
                          >
                            {el.content.slice(0, 40) || el.element_id}
                          </span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              )}
            </li>
          );
        })}
      </ul>
    </aside>
  );
}
