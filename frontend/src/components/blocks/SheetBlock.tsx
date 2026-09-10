import { AlertTriangle, FileSpreadsheet } from "lucide-react";
import { SheetDownload } from "../build/activityFeedParts/SheetDownload";

/**
 * SheetBlockComponent — renders a workbook PREVIEW card: sheet metadata + a
 * disclaimer that formulas are NOT evaluated (values are unvalidated — open in
 * a spreadsheet app to compute). The .xlsx itself is delivered as a workspace
 * artifact (ToolOutcome.artifacts → DeliverableEvent) and is downloaded through
 * the app's deliverable/workspace machinery, which holds the conversation id.
 *
 * D12: when a `cid` is threaded in (e.g. a future Build-coupled render path or
 * any caller that holds the conversation), the in-block download reuses the
 * ActivityFeed `SheetDownload` — the SAME component, not a fork. When no cid
 * is present (the standard research path), the button stays ABSENT — never a
 * false affordance (a self-fetching button with no cid would always 404). The
 * honest download lives where sheets are actually made: the Build/Agent
 * ActivityFeed via the declared-artifact route (app.py
 * `/conversations/{cid}/artifacts/{path}`, allowlisted to emitted .xlsx
 * artifacts) — see SheetDownload in build/ActivityFeed.tsx (rp-11 residue).
 *
 * Univer read-only embed (an in-card grid): still deferred. @univerjs/* 0.25.x
 * ships only a heavy collaborative editor with no standalone read-only component,
 * and a server-side openpyxl→grid preview runs on agent-overwritable bytes — out of
 * scope for now. The card stays an honest preview + the (cid-gated) download.
 */
export function SheetBlockComponent({
  title,
  filename,
  sheet_names,
  cid,
}: {
  title: string;
  filename: string;
  sheet_names: string[];
  /** The conversation id (cid) — when present, the in-block download resolves
   *  against the declared-artifact route. ABSENT = no download, no false
   *  affordance. */
  cid?: string | null;
}) {
  // Build the same shape ActivityFeed passes to SheetDownload; this is the
  // single source of truth for the download link/affordance.
  const sheet = { filename, title, sheet_names };
  return (
    <div className="my-inline rounded-card border border-hairline bg-surface-1">
      {/* Header — the in-block download appears here ONLY when a cid is present.
          Reuses SheetDownload verbatim (not a fork): same <a download>, same
          declared-artifact URL, same encodeURI semantics. */}
      <div className="flex items-center gap-inline border-b border-hairline px-body py-inline">
        <FileSpreadsheet className="size-5 shrink-0 text-accent" aria-hidden />
        <div className="min-w-0 flex-1">
          <div className="truncate font-ui text-[0.9rem] font-semibold text-text">{title}</div>
          <div className="truncate font-mono text-[0.72rem] text-text-faint">{filename}</div>
        </div>
        {cid && <SheetDownload sheet={sheet} conversationId={cid} />}
      </div>

      {/* Sheets list */}
      <div className="px-body py-inline">
        <div className="mb-hair font-ui text-[0.7rem] font-semibold uppercase tracking-wide text-text-faint">
          {sheet_names.length} sheet{sheet_names.length !== 1 ? "s" : ""}
        </div>
        <ul className="flex flex-wrap gap-hair">
          {sheet_names.map((name) => (
            <li
              key={name}
              className="rounded-control border border-hairline bg-surface-0 px-inline py-hair font-mono text-[0.78rem] text-text-muted"
            >
              {name}
            </li>
          ))}
        </ul>
      </div>

      {/* Provenance + formula disclaimer */}
      <div className="flex items-start gap-hair border-t border-hairline px-body py-inline">
        <AlertTriangle className="mt-px size-3.5 shrink-0 text-warn" aria-hidden />
        <p className="font-ui text-[0.74rem] leading-snug text-text-faint">
          Saved to the workspace as{" "}
          <span className="font-mono text-text-muted">{filename}</span>. Formulas are{" "}
          <strong className="font-semibold text-text-muted">not evaluated</strong> — open in a
          spreadsheet app (LibreOffice, Excel, Google Sheets) to compute values.
        </p>
      </div>
    </div>
  );
}
