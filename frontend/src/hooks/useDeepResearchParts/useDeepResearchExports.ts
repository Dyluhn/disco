/**
 * Deep Research report export, split out of `useDeepResearch`
 * (PKG-12-C/TS-0043/TS-0044): client-side markdown export plus the
 * server-side (pdf/docx) export path, with the pending signal the top-bar
 * export buttons key off of.
 */
import { useCallback, useState } from "react";
import {
  exportReportAsMarkdown,
  // Aliased: a local legacy `exportReport` (the MD-only button) used to SHADOW this
  // import, so pdf exports silently ran the markdown exporter instead.
  exportReport as exportReportApi,
  type ReportExportFmt,
} from "@/api/deepResearch";
import type { DeepResearchSession, DeepResearchStream } from "../useDeepResearchStream";

export function useDeepResearchExports(
  session: DeepResearchSession | null,
  stream: DeepResearchStream,
) {
  // fix-c #4: PDF/DOCX export runs on the server (WeasyPrint / pandoc) and can
  // take seconds — without a pending signal the button looked dead. MD stays
  // synchronous (client-side blob) so it never sets this.
  const [exportPending, setExportPending] = useState<ReportExportFmt | null>(null);

  const exportMd = useCallback(
    (followUps?: Array<[string, string]>) => {
      if (stream.report) exportReportAsMarkdown(stream.report, followUps);
    },
    [stream.report],
  );

  const exportReportByFmt = useCallback(
    async (fmt: ReportExportFmt, followUpSeqs?: number[]) => {
      if (!session?.cid) return;
      if (fmt === "md") {
        // Build [question, answer] pairs from followUpSeqs for client-side MD.
        // The seqs are user-message seqs; find them in stream.events.
        let followUps: Array<[string, string]> | undefined;
        if (followUpSeqs && followUpSeqs.length > 0 && stream.report) {
          const selected = new Set(followUpSeqs);
          const post = stream.events.filter(
            (e) =>
              e.kind === "message" &&
              (e.seq ?? 0) > (stream.report!.seq ?? -1),
          );
          const pairs: Array<[string, string]> = [];
          for (let i = 0; i < post.length; i++) {
            const e = post[i];
            if (
              e.kind === "message" &&
              e.message.role === "user" &&
              selected.has(e.seq ?? -1)
            ) {
              const next = post[i + 1];
              const answer =
                next && next.kind === "message" && next.message.role === "assistant"
                  ? (next.message.content ?? "")
                  : "";
              pairs.push([e.message.content ?? "", answer]);
            }
          }
          if (pairs.length > 0) followUps = pairs;
        }
        exportMd(followUps);
        return;
      }
      // Server-side export for pdf. The UI reports errors via toast/surface.
      // fix-c #4: mark pending around the await so the top-bar button can show
      // a spinner + disable itself (mirrors the ExportModal pattern in
      // NeedMoreCard — those buttons would deadlock-looking because the
      // server call takes seconds).
      setExportPending(fmt);
      try {
        await exportReportApi(session.cid, fmt, followUpSeqs);
      } finally {
        setExportPending(null);
      }
    },
    [session?.cid, exportMd, stream.report, stream.events],
  );

  // Legacy export (single-button MD download) — kept for backward compat.
  const exportReport = exportMd;

  return { exportPending, exportMd, exportReportByFmt, exportReport };
}
