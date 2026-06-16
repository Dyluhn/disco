import { useState } from "react";
import { ChevronLeft, ChevronRight, MonitorPlay } from "lucide-react";
import { SlidesDownload } from "../build/ActivityFeed";

/**
 * SlidesBlockComponent — renders a navigable slide-deck PREVIEW card. Each
 * slide the model emitted is shown in turn via prev/next controls; the actual
 * HTML / PDF / PPTX file is delivered as a workspace artifact
 * (ToolOutcome.artifacts → DeliverableEvent) and is downloaded through the
 * app's deliverable/workspace machinery, which holds the conversation id.
 *
 * D2: when a `cid` is threaded in, the in-block download reuses the
 * ActivityFeed `SlidesDownload` — the SAME component, not a fork. When no cid
 * is present (the standard research path), the button stays ABSENT — never a
 * false affordance (a self-fetching button with no cid would always 404). The
 * honest download lives where slides are actually made: the Build/Agent
 * ActivityFeed via the declared-artifact route (app.py
 * `/conversations/{cid}/artifacts/{path}`, allowlisted to emitted .html /
 * .pdf / .pptx artifacts) — see SlidesDownload in build/ActivityFeed.tsx
 * (rp-11 residue extended for slide decks).
 *
 * Inline embedding of the rendered HTML deck (an in-card iframe to the
 * artifact route) is still deferred: a PDF/PPTX in the same slot would 404
 * (the iframe can't render non-HTML), and a service-side slide rasteriser
 * (chromium→png/canvas) is out of scope. The card stays an honest preview
 * (prev/next through the inline `slides` list) + the (cid-gated) download.
 */
export function SlidesBlockComponent({
  title,
  filename,
  format,
  slide_count,
  slides,
  renderer,
  cid,
}: {
  title: string;
  filename: string;
  format: "html" | "pdf" | "pptx";
  slide_count: number;
  /** Inline per-slide preview, threaded for in-block navigation. Empty
   *  array → the viewer degrades to a "slide N of M" counter (still
   *  navigable, just no content body). */
  slides: { title?: string; content?: string }[];
  renderer: "marp" | "fallback";
  /** The conversation id (cid) — when present, the in-block download
   *  resolves against the declared-artifact route. ABSENT = no download,
   *  no false affordance. */
  cid?: string | null;
}) {
  // Total slide count prefers the explicit `slide_count` (authoritative —
  // comes from the slides backend's split), then falls back to the inline
  // `slides` array length. Always ≥ 1 so the counter never reads "0 / 0".
  const total = Math.max(slide_count || 0, slides.length, 1);
  const [index, setIndex] = useState(0);
  const safeIndex = Math.min(Math.max(index, 0), total - 1);
  const current = slides[safeIndex];
  // Build the same shape ActivityFeed passes to SlidesDownload; this is the
  // single source of truth for the download link/affordance.
  const slidesMeta = {
    filename,
    title,
    format,
    slide_count: total,
    slides,
  };
  const goPrev = () => setIndex((i) => (i - 1 + total) % total);
  const goNext = () => setIndex((i) => (i + 1) % total);
  return (
    <div className="my-inline rounded-card border border-hairline bg-surface-1">
      {/* Header — the in-block download appears here ONLY when a cid is
          present. Reuses SlidesDownload verbatim (not a fork): same
          <a download>, same declared-artifact URL, same encodeURI
          semantics. */}
      <div className="flex items-center gap-inline border-b border-hairline px-body py-inline">
        <MonitorPlay className="size-5 shrink-0 text-accent" aria-hidden />
        <div className="min-w-0 flex-1">
          <div className="truncate font-ui text-[0.9rem] font-semibold text-text">{title}</div>
          <div className="truncate font-mono text-[0.72rem] text-text-faint">
            {filename} · {format.toUpperCase()} · {total} slide{total !== 1 ? "s" : ""}
          </div>
        </div>
        {cid && <SlidesDownload slides={slidesMeta} conversationId={cid} />}
      </div>

      {/* Slide nav: prev / counter / next. The counter is the source of
          truth; the buttons are the affordance. Wraps to total slides so
          the user can step past either end. */}
      <div className="flex items-center justify-between gap-inline border-b border-hairline px-body py-inline">
        <button
          type="button"
          onClick={goPrev}
          disabled={total <= 1}
          aria-label="Previous slide"
          className="flex items-center gap-hair rounded-control border border-hairline bg-surface-0 px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:border-hairline-strong disabled:cursor-not-allowed disabled:opacity-40"
        >
          <ChevronLeft className="size-3.5" aria-hidden />
          Prev
        </button>
        <div
          className="font-mono text-[0.78rem] text-text-faint"
          aria-live="polite"
          aria-label={`Slide ${safeIndex + 1} of ${total}`}
        >
          <span className="text-text-muted">{safeIndex + 1}</span>
          <span className="mx-1 text-text-faint">/</span>
          <span>{total}</span>
        </div>
        <button
          type="button"
          onClick={goNext}
          disabled={total <= 1}
          aria-label="Next slide"
          className="flex items-center gap-hair rounded-control border border-hairline bg-surface-0 px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:border-hairline-strong disabled:cursor-not-allowed disabled:opacity-40"
        >
          Next
          <ChevronRight className="size-3.5" aria-hidden />
        </button>
      </div>

      {/* Active slide preview. Renders the inline slide content when the
          model emitted it; otherwise the slide body is the format / counter
          (still honest — the user knows the deck has N slides and can step
          through). */}
      <div className="px-body py-inline">
        {current?.title && (
          <div className="mb-hair font-ui text-[0.72rem] font-semibold uppercase tracking-wide text-text-faint">
            {current.title}
          </div>
        )}
        {current?.content ? (
          <p className="prose-reading whitespace-pre-wrap text-[0.95rem]">
            {current.content}
          </p>
        ) : (
          <p className="font-ui text-[0.82rem] italic text-text-faint">
            Slide {safeIndex + 1} of {total} ({(format || "html").toUpperCase()} ·{" "}
            {renderer === "fallback" ? "fallback renderer" : "Marp-rendered"}).{" "}
            {cid
              ? "Download above to view the rendered slide."
              : "Open the deck in the build surface to view the rendered slide."}
          </p>
        )}
      </div>

      {/* Provenance + renderer hint */}
      <div className="flex items-start gap-hair border-t border-hairline px-body py-inline">
        <MonitorPlay className="mt-px size-3.5 shrink-0 text-text-faint" aria-hidden />
        <p className="font-ui text-[0.74rem] leading-snug text-text-faint">
          Saved to the workspace as{" "}
          <span className="font-mono text-text-muted">{filename}</span>. Rendered by{" "}
          {renderer === "fallback" ? "the HTML fallback renderer" : "Marp"}.
        </p>
      </div>
    </div>
  );
}
