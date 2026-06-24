/**
 * SlideThumbnail — a faithful, non-interactive mini-render of ONE slide for the
 * deck editor's left rail.
 *
 * Why an iframe (not a coloured rectangle): the rail used to paint each slot with
 * the raw model `bg_color` (e.g. dark navy) + a number. But the THEME (e.g. the
 * Disco template) makes the actual rendered slides LIGHT with real content, so the
 * raw-bg swatches showed up as black/dark blanks that looked nothing like the main
 * preview. This component instead mounts the SAME `renderHtml` the main canvas uses
 * (`SlideCanvas`), activates this slide via the shared `slideToggle`, and CSS-scales
 * the 16:9 render down to fill the thumbnail box — so the rail visually MATCHES the
 * main preview.
 *
 * Security/posture mirrors SlideCanvas: `sandbox="allow-same-origin"` and NO
 * `allow-scripts` — the iframe's own nav script is disabled; React owns nav. The
 * iframe is display-only (`pointer-events: none`, `tabIndex=-1`); the parent
 * `<button>` owns the click that selects the slide.
 *
 * @module SlideThumbnail
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { activateSlide } from "./slideToggle";

// Base render resolution (16:9). The iframe is laid out at this fixed size and then
// CSS-scaled by (measured box width / BASE_W), so the slide's internal layout is
// faithful regardless of how narrow the rail is.
const BASE_W = 1280;
const BASE_H = 720;

interface SlideThumbnailProps {
  /** The full rendered deck HTML (same srcDoc the main SlideCanvas uses). */
  renderHtml: string | null;
  /** Which slide this thumbnail shows (drives the .slide.active toggle). */
  slideId: string;
}

export function SlideThumbnail({ renderHtml, slideId }: SlideThumbnailProps) {
  const clipRef = useRef<HTMLDivElement | null>(null);
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const [scale, setScale] = useState(0);

  // Activate the correct slide once the iframe finishes loading its srcDoc.
  const handleLoad = useCallback(() => {
    activateSlide(iframeRef.current?.contentDocument, slideId);
  }, [slideId]);

  // Re-activate if the slide id (deck reorder) or the render changes without a fresh
  // load — keeps the thumbnail pinned to its own slide.
  useEffect(() => {
    activateSlide(iframeRef.current?.contentDocument, slideId);
  }, [slideId, renderHtml]);

  // Measure the clip box to compute the down-scale factor; stays responsive to rail
  // width via ResizeObserver.
  useEffect(() => {
    const node = clipRef.current;
    if (!node) return;
    const apply = () => {
      const w = node.clientWidth;
      if (w > 0) setScale(w / BASE_W);
    };
    apply();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(apply);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  // Before the render HTML arrives, show a neutral LIGHT placeholder — never a black
  // raw-bg_color swatch (the bug this component fixes).
  if (!renderHtml) {
    return (
      <div
        ref={clipRef}
        style={{ position: "absolute", inset: 0, background: "#fff" }}
        aria-hidden="true"
      />
    );
  }

  return (
    <div
      ref={clipRef}
      style={{
        position: "absolute",
        inset: 0,
        overflow: "hidden",
        background: "#fff",
        pointerEvents: "none", // clicks fall through to the parent <button>
      }}
      aria-hidden="true"
    >
      <iframe
        ref={iframeRef}
        srcDoc={renderHtml}
        // allow-same-origin (read contentDocument for the toggle), NO allow-scripts.
        sandbox="allow-same-origin"
        onLoad={handleLoad}
        tabIndex={-1}
        title={`Slide ${slideId} thumbnail`}
        style={{
          position: "absolute",
          top: 0,
          left: 0,
          width: `${BASE_W}px`,
          height: `${BASE_H}px`,
          border: "none",
          transform: `scale(${scale})`,
          transformOrigin: "top left",
          pointerEvents: "none",
        }}
      />
    </div>
  );
}
