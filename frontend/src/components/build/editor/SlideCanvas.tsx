/**
 * SlideCanvas — 16:9 iframe substrate + transparent overlay layer for the WYSIWYG editor.
 *
 * Architecture (§5d):
 *  - Visual layer: `<iframe srcDoc={renderHtml}>` fills the 16:9 container. The iframe
 *    shows the REAL rendered deck (brand fonts, accent, bg, chrome). We drive which slide
 *    is active by toggling `.slide.active` inside the iframe DOM (no `allow-scripts` —
 *    the iframe's own navigation script is disabled; the editor owns navigation).
 *  - Edit layer: `<ElementBox>` overlays, TRANSPARENT, positioned from measured
 *    `getBoundingClientRect()` values. Only elements stamped with `data-element-id` in
 *    the render HTML get overlays. Clicks/double-clicks land on the overlays, not the
 *    iframe.
 *
 * Measurement lifecycle:
 *  1. On mount: initialize zero-rect overlays from the elementMap for the active slide
 *     so overlay DOM nodes exist immediately (required for jsdom tests).
 *  2. On iframe `onLoad`: toggle active slide in iframe DOM, measure rects, update specs.
 *  3. On `activeSlideId` change: reset to zero-rect, then measure.
 *  4. On container resize (ResizeObserver, debounced 120 ms): re-measure.
 *  5. Guard: if measurement returns 0 elements (jsdom / iframe not yet ready), keep the
 *     zero-rect initial specs so DOM nodes remain accessible for tests.
 *
 * @module SlideCanvas
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ElementBox } from "./ElementBox";
import { activateSlide } from "./slideToggle";
import type { JsonPatchOp, LoweredElement, OverlaySpec } from "./types";

// ─── Types ───────────────────────────────────────────────────────────────────

export interface ElementMapEntry {
  json_pointer: string;
  kind: LoweredElement["kind"];
  content: string;
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

// Only TEXT elements are overlay-editable. Charts/tables/images/chrome are shown by
// the iframe (the real render) and are NOT given an editable overlay — a click-to-edit
// box over a chart that wrote `/slides/N/title` would be a false affordance. Their
// selection lives in the LayersPanel, not as a spatial overlay.
const EDITABLE_KINDS = new Set<LoweredElement["kind"]>(["title", "subtitle", "bullet"]);

/**
 * Build zero-rect overlay specs for the active slide's EDITABLE text elements.  These
 * are the initial placeholders shown before the iframe loads + rects are measured —
 * they ensure DOM nodes exist in tests even when jsdom cannot render the iframe srcdoc.
 */
function buildZeroSpecs(
  elementMap: Map<string, ElementMapEntry>,
  activeSlideId: string,
): OverlaySpec[] {
  const prefix = activeSlideId + ":";
  const specs: OverlaySpec[] = [];
  for (const [eid, entry] of elementMap) {
    if (eid.startsWith(prefix) && EDITABLE_KINDS.has(entry.kind)) {
      specs.push({
        element_id: eid,
        json_pointer: entry.json_pointer,
        kind: entry.kind,
        content: entry.content,
        rect: { left: 0, top: 0, width: 0, height: 0 },
      });
    }
  }
  return specs;
}

// ─── Props ───────────────────────────────────────────────────────────────────

interface SlideCanvasProps {
  /** srcDoc for the iframe — the full rendered deck HTML from render_html(). */
  renderHtml: string | null;
  /** The slide currently shown in the editor. Drives the iframe's .slide.active toggle. */
  activeSlideId: string;
  /** element_id → {json_pointer, kind, content} from the LoweredDeck. */
  elementMap: Map<string, ElementMapEntry>;
  selectedElementId: string | null;
  onSelectElement: (elementId: string) => void;
  onPatch: (patch: JsonPatchOp[]) => void;
}

// ─── SlideCanvas ─────────────────────────────────────────────────────────────

export function SlideCanvas({
  renderHtml,
  activeSlideId,
  elementMap,
  selectedElementId,
  onSelectElement,
  onPatch,
}: SlideCanvasProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const debounceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Start with zero-rect overlays so DOM nodes exist immediately (tests / pre-load state).
  const [overlaySpecs, setOverlaySpecs] = useState<OverlaySpec[]>(() =>
    buildZeroSpecs(elementMap, activeSlideId),
  );

  /**
   * Core measurement function: toggle the active slide class inside the iframe DOM,
   * then querySelectorAll('[data-element-id]') and measure each rect relative to the
   * iframe container.  Only elements whose element_id is in the elementMap get an
   * overlay.  If measurement yields 0 specs (iframe not yet ready / jsdom), we keep
   * the existing zero-rect specs intact so tests can still find the DOM nodes.
   */
  const measure = useCallback((): void => {
    const iframe = iframeRef.current;
    if (!iframe) return;
    const doc = iframe.contentDocument;
    if (!doc || !doc.body) return;

    // Show only the active slide (shared with SlideThumbnail via slideToggle so the
    // two iframes can't drift). If absent, the iframe content isn't ready yet (e.g.
    // jsdom doesn't render srcdoc) — bail and KEEP the zero-rect specs so DOM nodes
    // stay alive for tests.
    const activeSection = activateSlide(doc, activeSlideId);
    if (!activeSection) return;

    // Measure ONLY this slide's stamped EDITABLE text elements. getBoundingClientRect
    // from contentDocument is already iframe-VIEWPORT-relative, which equals the overlay
    // container's coordinate space (both are inset:0 in the same box) — so use the rect
    // DIRECTLY; subtracting the parent-page iframe offset would double-shift it.
    const stamped = activeSection.querySelectorAll<HTMLElement>("[data-element-id]");
    const newSpecs: OverlaySpec[] = [];
    stamped.forEach((el) => {
      const eid = el.getAttribute("data-element-id");
      if (!eid) return;
      const entry = elementMap.get(eid);
      if (!entry || !EDITABLE_KINDS.has(entry.kind)) return; // non-text → no overlay
      const r = el.getBoundingClientRect();
      newSpecs.push({
        element_id: eid,
        json_pointer: entry.json_pointer,
        kind: entry.kind,
        content: entry.content,
        rect: { left: r.left, top: r.top, width: r.width, height: r.height },
      });
    });

    // Active section found ⇒ iframe is ready ⇒ this measured set is AUTHORITATIVE
    // (set even if empty — clears stale zero-rect overlays from a prior slide or a
    // non-text slide so they can't linger as off-screen focusable buttons).
    setOverlaySpecs(newSpecs);
  }, [activeSlideId, elementMap]);

  // Debounced wrapper for the ResizeObserver (avoids thrashing on rapid resize).
  const debouncedMeasure = useMemo(
    () => () => {
      if (debounceTimerRef.current) clearTimeout(debounceTimerRef.current);
      debounceTimerRef.current = setTimeout(measure, 120);
    },
    [measure],
  );

  // When the active slide changes: reset to zero-rect for the new slide, then measure.
  useEffect(() => {
    setOverlaySpecs(buildZeroSpecs(elementMap, activeSlideId));
    measure(); // no-op if iframe not yet ready; onLoad handles that case
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSlideId, elementMap]); // intentionally omit `measure` to avoid double-run

  // Wire ResizeObserver for responsive re-measurement.
  useEffect(() => {
    const container = containerRef.current;
    if (!container || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(debouncedMeasure);
    observer.observe(container);
    return () => {
      observer.disconnect();
      // Also drop any pending debounced measure so a resize scheduled before a slide
      // change can't later fire stale measurement against the new closure.
      if (debounceTimerRef.current) clearTimeout(debounceTimerRef.current);
    };
  }, [debouncedMeasure]);

  // Clean up the debounce timer on unmount.
  useEffect(
    () => () => {
      if (debounceTimerRef.current) clearTimeout(debounceTimerRef.current);
    },
    [],
  );

  const handleIframeLoad = useCallback(() => {
    measure();
  }, [measure]);

  return (
    /*
     * 16:9 container via padding-top trick.  The iframe fills it absolutely.
     * The overlay div sits on top (pointer-events:none on the container; each
     * ElementBox restores pointer-events:auto for its own area).
     */
    <div
      ref={containerRef}
      style={{
        position: "relative",
        width: "100%",
        paddingTop: "56.25%", // 9 / 16 = 0.5625
        background: "#111",
        borderRadius: "4px",
        overflow: "hidden",
      }}
      aria-label={`Slide ${activeSlideId}`}
    >
      {/* No render yet (still fetching, slow 2.5 MB load, or a transient failure):
          show an explicit state — NEVER a silent black box that reads as "broken". */}
      {!renderHtml && (
        <div
          role="status"
          style={{
            position: "absolute",
            inset: 0,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            color: "#999",
            fontFamily: "var(--ui, system-ui, sans-serif)",
            fontSize: "0.9rem",
          }}
        >
          Loading slide preview…
        </div>
      )}

      {/* ── Visual layer: the real rendered deck in an iframe ──────────────── */}
      <iframe
        ref={iframeRef}
        srcDoc={renderHtml ?? ""}
        // allow-same-origin: lets the parent read contentDocument for measurement.
        // No allow-scripts: the iframe's own slide-nav script is disabled; we own nav.
        sandbox="allow-same-origin"
        onLoad={handleIframeLoad}
        style={{
          position: "absolute",
          inset: 0,
          width: "100%",
          height: "100%",
          border: "none",
        }}
        title="Slide preview"
      />

      {/* ── Overlay layer: transparent click-to-edit ElementBox hotspots ──── */}
      <div
        style={{
          position: "absolute",
          inset: 0,
          pointerEvents: "none", // clicks on empty areas fall through to the iframe
        }}
      >
        {overlaySpecs.map((spec) => (
          <ElementBox
            key={spec.element_id}
            elementId={spec.element_id}
            jsonPointer={spec.json_pointer}
            kind={spec.kind}
            content={spec.content}
            rect={spec.rect}
            selected={selectedElementId === spec.element_id}
            onSelect={onSelectElement}
            onPatch={onPatch}
          />
        ))}
      </div>
    </div>
  );
}
