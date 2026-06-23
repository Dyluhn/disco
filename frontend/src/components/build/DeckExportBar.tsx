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
 *   GET /conversations/{cid}/deck/export?path={base}&template={id}&fmt={pptx|html|pdf}
 * which re-renders the authored deck with the chosen template at download time.
 *
 * HONEST AFFORDANCES: `pptx` and `html` render in-process and are always offered.
 * `pdf` (W-22) is themed too, but renders the .pptx then converts it to PDF with
 * headless LibreOffice inside a THROWAWAY sandbox — which requires a CONTAINER backend
 * (the sandbox image ships LibreOffice; the host self-hoster may not). So the PDF link
 * is shown ONLY when the active sandbox backend can produce it (gvisor/local/podman);
 * on the host-`process` backend it is hidden (the route would 409) — exactly the same
 * honesty as pptx/html, no dead/404 affordance.
 *
 * @module DeckExportBar
 */

import { useEffect, useState } from "react";
import { Download } from "lucide-react";
import { cn } from "@/lib/cn";
import { agentHttpBase, agentLive } from "@/api/client";
import { useTemplates } from "@/hooks/useTemplates";
import { deckPdfCapableBackend } from "@/lib/isolation";
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

type ExportFmt = "pptx" | "html" | "pdf";

/** Always-on formats (in-process render). `pdf` is appended only when the active
 *  sandbox backend can produce it — see module docstring (no false affordance). */
const FORMATS: { fmt: ExportFmt; label: string }[] = [
  { fmt: "pptx", label: "PowerPoint (.pptx)" },
  { fmt: "html", label: "Web page (.html)" },
];

const PDF_FORMAT: { fmt: ExportFmt; label: string } = { fmt: "pdf", label: "PDF (.pdf)" };

export function DeckExportBar({ conversationId, base, title, className }: DeckExportBarProps) {
  const templates = useTemplates();
  const [templateId, setTemplateId] = useState("disco-light");

  // W-22: read the live sandbox backend once (it's stable for a run) so we can gate the
  // PDF affordance — PDF deck export needs a container backend (LibreOffice in the image).
  const [sandboxBackend, setSandboxBackend] = useState<string | null>(null);
  useEffect(() => {
    if (!conversationId || !agentLive()) return;
    void fetch(`${agentHttpBase()}/conversations/${conversationId}/state`)
      .then((r) => r.json())
      .then((s: { sandbox_backend?: string }) => setSandboxBackend(s.sandbox_backend ?? null))
      .catch(() => {});
  }, [conversationId]);

  const formats = deckPdfCapableBackend(sandboxBackend) ? [...FORMATS, PDF_FORMAT] : FORMATS;

  // The jailed deck base path the /deck/export route expects (a plain name, no "/").
  const exportHref = (fmt: ExportFmt) =>
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
        {formats.map(({ fmt, label }) => (
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
