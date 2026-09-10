/**
 * ActivityFeed — the generated-deck download card, extracted verbatim from
 * ActivityFeed.tsx so the feed module stays inside the logical-line budget.
 * Same component, same routes, same markup: not a fork.
 */

import { AlertTriangle, Download, MonitorPlay } from "lucide-react";
import { artifactUrl } from "@/api/artifacts";
import { DeckExportBar } from "@/components/build/DeckExportBar";
import { rendererLabel } from "@/lib/slidesRenderer";
import type { ActivityItem } from "@/lib/buildTrace";

/** A generated slide deck — a real download via the declared-artifact route
 * (encodeURI preserves any subdir slashes). Honest: only renders when there's a
 * conversation id to fetch against; the file is whatever the slides backend
 * emitted (HTML / PDF / PPTX), and the format is shown so the user knows what
 * they'll get.
 *
 * Exported (D2) so SlidesBlock (AnswerDocument path) can reuse the EXACT same
 * download affordance when a cid is threaded down — same pattern as
 * SheetDownload: not a fork, no divergence. The declared-artifact route is
 * `/conversations/{cid}/artifacts/{filename}` (app.py; allowlisted to emitted
 * .html/.pdf/.pptx artifacts) — the same route the .xlsx download uses. */
export function SlidesDownload({
  slides,
  conversationId,
}: {
  slides: NonNullable<ActivityItem["expandable"]>["slides"];
  conversationId: string;
}) {
  if (!slides) return null;
  const n = slides.slide_count ?? slides.slides?.length ?? 0;
  // An editable deck (authored sidecar) can be re-rendered with any template at
  // download time via /deck/export — pure render-on-demand, no live sandbox needed.
  // A non-editable deck (Marp) keeps the baked artifact link (no false affordance).
  const editable = Boolean(slides.editable && slides.base);
  // R7: be honest about a DEGRADED fallback deck (Marp CLI unavailable) vs a real
  // structured render — otherwise the user can't tell a real deck from "HTML fake slides".
  const rl = rendererLabel(slides.renderer);
  const meta =
    n > 0 ? `${slides.filename} · ${n} slide${n !== 1 ? "s" : ""}` : slides.filename;

  // W-16: an editable deck → the SHARED DeckExportBar (Theme picker + real pptx/html
  // render-on-demand download links). One component, also used by the in-app editor.
  if (editable) {
    return (
      <div className="mt-hair flex flex-col gap-hair">
        {!rl.real && (
          <p className="flex items-center gap-hair font-ui text-[0.7rem] text-unsupported">
            <AlertTriangle className="size-3 shrink-0" aria-hidden />
            Built with {rl.long} — not the full structured deck.
          </p>
        )}
        <DeckExportBar
          conversationId={conversationId}
          base={slides.base!}
          title={slides.title || slides.filename}
        />
        <span className="truncate px-inline font-mono text-[0.7rem] text-text-faint">{meta}</span>
      </div>
    );
  }

  // Non-editable deck (Marp/fallback) → the baked artifact link (no template re-render).
  const staticHref = artifactUrl(conversationId, slides.filename);
  const fmt = (slides.format || "html").toUpperCase();
  return (
    <div className="mt-hair flex flex-col gap-hair">
      <a
        href={staticHref}
        download
        data-disco-control="build.activity-download"
        data-download-kind="slides"
        className="flex items-center gap-inline rounded-card border border-hairline bg-surface-0 px-inline py-hair transition-colors hover:border-hairline-strong"
      >
        <MonitorPlay className="size-4 shrink-0 text-accent" aria-hidden />
        <span className="min-w-0 flex-1">
          <span className="block truncate font-ui text-[0.82rem] text-text">
            {slides.title || slides.filename}
          </span>
          <span className="block truncate font-mono text-[0.7rem] text-text-faint">
            {slides.filename}
            {n > 0 ? ` · ${fmt} · ${n} slide${n !== 1 ? "s" : ""}` : ` · ${fmt}`}
          </span>
        </span>
        <Download className="size-3.5 shrink-0 text-text-faint" aria-hidden />
      </a>
      {!rl.real && (
        <p className="flex items-center gap-hair font-ui text-[0.7rem] text-unsupported">
          <AlertTriangle className="size-3 shrink-0" aria-hidden />
          Built with {rl.long} — not the full structured deck.
        </p>
      )}
    </div>
  );
}
