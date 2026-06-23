/**
 * DeckExportBar — the SHARED deck export affordance (W-16/W-19).
 *
 * One component, two homes:
 *   1. ActivityFeed's SlidesDownload — shown under a generated, editable deck.
 *   2. The in-app deck editor (DeckEditorPane) — a header above DeckEditor so the
 *      user can re-export the deck they're editing without leaving the tab (W-19).
 *
 * It renders a first-class "Theme" picker (the brand-template spin) plus the real
 * download links. The links hit the render-on-demand route
 *   GET /conversations/{cid}/deck/export?path={base}&template={id}&fmt={pptx|html}
 * which re-renders the authored deck with the chosen template at download time — no
 * live sandbox required.
 *
 * HONEST AFFORDANCES: the backend deck export currently supports ONLY `pptx` and
 * `html`. There is deliberately NO PDF link here — a PDF affordance now would 404
 * (a false affordance). PDF deck export arrives with W-22; add its link then.
 *
 * @module DeckExportBar
 */

import { useState } from "react";
import { Download } from "lucide-react";
import { cn } from "@/lib/cn";
import { agentHttpBase } from "@/api/client";
import { useTemplates } from "@/hooks/useTemplates";
import { TemplatePicker } from "@/components/research/TemplatePicker";

interface DeckExportBarProps {
  /** The conversation that owns the deck — the export route is scoped to it. */
  conversationId: string;
  /** The deck base name (no extension) — the jailed `path` the export route expects. */
  base: string;
  /** Optional heading (e.g. the deck title) shown above the Theme row. */
  title?: string;
  className?: string;
}

const LINK_BTN =
  "flex items-center gap-hair rounded-control border border-hairline bg-surface-1 px-inline py-px " +
  "font-ui text-[0.74rem] text-text-muted transition-colors hover:border-hairline-strong hover:text-text";

/** The two export formats the backend actually supports. NO `pdf` — see module
 *  docstring (PDF deck export lands with W-22; a link now would be a dead 404). */
const FORMATS: { fmt: "pptx" | "html"; label: string }[] = [
  { fmt: "pptx", label: "PowerPoint (.pptx)" },
  { fmt: "html", label: "Web page (.html)" },
];

export function DeckExportBar({ conversationId, base, title, className }: DeckExportBarProps) {
  const templates = useTemplates();
  const [templateId, setTemplateId] = useState("disco-light");

  // The jailed deck base path the /deck/export route expects (a plain name, no "/").
  const exportHref = (fmt: "pptx" | "html") =>
    `${agentHttpBase()}/conversations/${conversationId}/deck/export` +
    `?path=${encodeURIComponent(base)}` +
    `&template=${encodeURIComponent(templateId)}&fmt=${fmt}`;

  return (
    <div
      data-disco-control="build.deck-export-bar"
      className={cn(
        "flex flex-col gap-hair rounded-card border border-hairline bg-surface-0 px-inline py-hair",
        className,
      )}
    >
      {title && (
        <span className="truncate font-ui text-[0.82rem] font-medium text-text">{title}</span>
      )}
      {/* Theme row — first-class (promoted from the old inline TemplatePicker). */}
      <TemplatePicker
        templates={templates}
        value={templateId}
        onChange={setTemplateId}
        label="Theme"
        id={`deck-template-${base}`}
        dataControl="build.deck-export-template"
      />
      {/* Real download links — render-on-demand with the chosen template. */}
      <div className="flex flex-wrap items-center gap-hair px-inline">
        {FORMATS.map(({ fmt, label }) => (
          <a
            key={fmt}
            href={exportHref(fmt)}
            download
            data-disco-control="build.deck-export"
            data-export-fmt={fmt}
            className={LINK_BTN}
          >
            <Download className="size-3.5 shrink-0" aria-hidden />
            {label}
          </a>
        ))}
      </div>
    </div>
  );
}
