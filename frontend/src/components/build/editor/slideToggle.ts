/**
 * slideToggle — the ONE place that knows how to show a single slide inside a
 * rendered-deck iframe.
 *
 * The render_html() output is a multi-slide document where every slide is a
 * `.slide` element carrying a `data-slide-id`; exactly one slide carries the
 * `.active` class (shown) while the rest are hidden by CSS. Because the editor
 * disables the iframe's own nav script (no `allow-scripts`), the React side owns
 * which slide is active by toggling that class in the iframe DOM.
 *
 * Both surfaces that drive an iframe — `SlideCanvas` (the big main preview) and
 * `SlideThumbnail` (each rail thumbnail) — call THIS function so they can never
 * drift in how they pick or activate a slide.
 *
 * @module slideToggle
 */

/**
 * Show only the slide with `slideId` inside `doc`: remove `.active` from every
 * `.slide`, then add it to the matching slide section.
 *
 * @returns the activated section element, or `null` when the document isn't ready
 *   (still loading / jsdom can't render srcdoc) or the id isn't present — callers
 *   treat `null` as "iframe not ready yet, leave current state".
 */
export function activateSlide(
  doc: Document | null | undefined,
  slideId: string,
): HTMLElement | null {
  if (!doc || !doc.body) return null;
  // The slide section is the first node with this data-slide-id (child elements
  // re-stamp the same id; the section precedes them in document order).
  const activeSection = doc.querySelector<HTMLElement>(`[data-slide-id="${slideId}"]`);
  if (!activeSection) return null;
  doc.querySelectorAll(".slide").forEach((el) => el.classList.remove("active"));
  activeSection.classList.add("active");
  return activeSection;
}
