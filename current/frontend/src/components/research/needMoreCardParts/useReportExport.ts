/**
 * NeedMoreCard — export state/logic hook, extracted from ExportModal.
 *
 * `handleMd` and `handleFmt` were near-duplicates (build body → fetch blob →
 * FSA-save-or-anchor-download → catch AbortError → clear); this collapses
 * both into one `runExport(fmt, ext, mime)` and keeps the two as trivial
 * wrappers so ExportModal's JSX (and its onClick call sites) is unchanged.
 */

import { useCallback, useState } from "react";
import { fetchReportExportBlob } from "@/api/deepResearch";
import { downloadBlob, hasFSA, saveViaPicker } from "./fileSave";
import { sanitizeFilename } from "./filename";

export interface UseReportExportOptions {
  cid: string;
  /** The report's title/query — the filename is derived from it (B4). */
  reportQuery: string;
  /** Selected follow-up seqs to include in the export (WALK-20). */
  followUpSeqs?: number[];
  /** PDF template selection, threaded into the export POST body. Ignored by
   * the server for `md`. */
  themeName: string;
  themeMode: string;
}

export interface UseReportExportResult {
  exporting: "md" | "pdf" | null;
  exportError: string | null;
  /** Whether the File System Access save-picker is available (drives the
   * dialog's descriptive copy). */
  fsa: boolean;
  handleMd: () => Promise<void>;
  handleFmt: (fmt: "pdf") => Promise<void>;
}

export function useReportExport({
  cid,
  reportQuery,
  followUpSeqs,
  themeName,
  themeMode,
}: UseReportExportOptions): UseReportExportResult {
  const [exporting, setExporting] = useState<"md" | "pdf" | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const fsa = hasFSA();

  const hasFollowUps = Boolean(followUpSeqs && followUpSeqs.length > 0);
  const baseFilename = sanitizeFilename(reportQuery);

  const runExport = useCallback(
    async (fmt: "md" | "pdf", ext: string, mimeType: string) => {
      setExporting(fmt);
      setExportError(null);
      // Thread theme+mode so PDF honours the chosen template; MD ignores it
      // server-side (byte-identical regardless).
      const exportBody: { follow_up_seqs?: number[]; theme: string; mode: string } = {
        theme: themeName,
        mode: themeMode,
      };
      if (hasFollowUps) exportBody.follow_up_seqs = followUpSeqs;
      const bodyPayload = JSON.stringify(exportBody);

      try {
        const blob = await fetchReportExportBlob(cid, fmt, bodyPayload);
        if (fsa) {
          await saveViaPicker(blob, {
            suggestedName: baseFilename + ext,
            types: [
              {
                description: fmt === "md" ? "Markdown" : fmt.toUpperCase(),
                accept: { [mimeType]: [ext] },
              },
            ],
          });
        } else {
          downloadBlob(blob, baseFilename + ext);
        }
      } catch (e: unknown) {
        if (!(e instanceof DOMException && e.name === "AbortError")) {
          setExportError(e instanceof Error ? e.message : String(e));
        }
      } finally {
        setExporting(null);
      }
    },
    [cid, fsa, hasFollowUps, followUpSeqs, baseFilename, themeName, themeMode],
  );

  const handleMd = useCallback(async () => {
    await runExport("md", ".md", "text/markdown");
  }, [runExport]);

  const handleFmt = useCallback(
    async (fmt: "pdf") => {
      await runExport(fmt, ".pdf", "application/pdf");
    },
    [runExport],
  );

  return { exporting, exportError, fsa, handleMd, handleFmt };
}
