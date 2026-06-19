/**
 * SlideCanvas — renders one slide as a 16:9 canvas with positioned ElementBoxes.
 *
 * Each `LoweredElement` in `slide.elements[]` is rendered as an absolutely-
 * positioned `ElementBox`. The canvas itself is a `position:relative` container
 * that maintains the 16:9 aspect ratio via `padding-top: 56.25%`.
 *
 * `data-slide-id` is stamped on the canvas div so the selection overlay can
 * identify the slide boundary when walking up.
 *
 * @module SlideCanvas
 */

import { useRef } from "react";
import { ElementBox } from "./ElementBox";
import type { JsonPatchOp, LoweredSlide } from "./types";

interface SlideCanvasProps {
  slide: LoweredSlide;
  /** The element_id of the currently selected element, or null. */
  selectedElementId: string | null;
  onSelectElement: (elementId: string) => void;
  onPatch: (patch: JsonPatchOp[]) => void;
  /** Visual scale factor for the canvas (1 = full size). */
  scale?: number;
  /** A2: disable element drag (text-edit-only) — threaded to each ElementBox. */
  disableDrag?: boolean;
}

export function SlideCanvas({
  slide,
  selectedElementId,
  onSelectElement,
  onPatch,
  scale = 1,
  disableDrag = false,
}: SlideCanvasProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);

  // We need the canvas px dimensions for drag-to-% conversion.
  // These are read lazily from the DOM on drag start — no ResizeObserver overhead.
  const getCanvasDimensions = (): { w: number; h: number } => {
    const el = containerRef.current;
    if (!el) return { w: 1600, h: 900 };
    const rect = el.getBoundingClientRect();
    return { w: rect.width, h: rect.height };
  };

  return (
    /*
     * Outer wrapper: maintains 16:9 via padding-top trick, clips overflow.
     * `position:relative` is the layout parent for ElementBox absolute positions.
     */
    <div
      ref={containerRef}
      data-slide-id={slide.slide_id}
      style={{
        position: "relative",
        width: "100%",
        paddingTop: "56.25%",   // 9/16 = 56.25 %
        background: slide.bg_color,
        overflow: "hidden",
        transform: scale !== 1 ? `scale(${scale})` : undefined,
        transformOrigin: "top left",
        flexShrink: 0,
      }}
      aria-label={`Slide ${slide.slide_idx + 1}`}
    >
      {/* Absolute-fill inner layer: this is what ElementBox positions against */}
      <div
        style={{
          position: "absolute",
          inset: 0,
        }}
      >
        {slide.elements.map((el) => (
          <ElementBox
            key={el.element_id}
            element={el}
            selected={selectedElementId === el.element_id}
            canvasWidth={getCanvasDimensions().w}
            canvasHeight={getCanvasDimensions().h}
            onSelect={onSelectElement}
            onPatch={onPatch}
            disableDrag={disableDrag}
          />
        ))}
      </div>
    </div>
  );
}
