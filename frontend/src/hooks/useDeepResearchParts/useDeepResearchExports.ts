/**
 * Deep Research report export, split out of `useDeepResearch`
 * (PKG-12-C/TS-0043/TS-0044): one server-side Markdown/PDF path, with the
 * pending signal the top-bar export buttons key off of.
 */
import { useCallback, useState } from "react";
import {
  exportReport as exportReportApi,
  type ReportExportFmt,
} from "@/api/deepResearch";
import type { DeepResearchSession } from "../useDeepResearchStream";

export function useDeepResearchExports(
  session: DeepResearchSession | null,
) {
  // Exports run on the server so every format receives the canonical generated
  // title and selected follow-up turns.
  // They can take seconds — without a pending signal the button looked dead.
  const [exportPending, setExportPending] = useState<ReportExportFmt | null>(null);

  const exportReportByFmt = useCallback(
    async (fmt: ReportExportFmt, followUpSeqs?: number[]) => {
      if (!session?.cid) return;
      // The UI reports errors via toast/surface.
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
    [session?.cid],
  );

  const exportMd = useCallback(
    () => exportReportByFmt("md"),
    [exportReportByFmt],
  );

  // Legacy export (single-button MD download) — kept for backward compat.
  const exportReport = exportMd;

  return { exportPending, exportMd, exportReportByFmt, exportReport };
}
