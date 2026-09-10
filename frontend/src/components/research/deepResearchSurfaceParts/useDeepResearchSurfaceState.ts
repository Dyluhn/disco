/**
 * Surface-local state + callbacks for DeepResearchSurface — relocated verbatim
 * (same bodies, same dep arrays, same relative call order) from the parent's
 * top-of-function block. See DeepResearchSurface.tsx's header for the surface
 * map.
 *
 * Kept as ONE hook (rather than split at the seams the original code visually
 * suggested) specifically so the parent calls exactly one sub-hook, in the one
 * position the original block of hook calls occupied — no reordering relative
 * to `useDeepResearch`, and no risk of a hook call becoming conditional.
 */
import { useCallback, useEffect, useState } from "react";
import { publishRunStatus } from "@/lib/runStatusBridge";
import { markAllModesFresh } from "@/lib/sessionResume";
import type { useDeepResearch } from "@/hooks/useDeepResearch";
import { useDeepResearchDoneNotification } from "@/hooks/useDeepResearchDoneNotification";
import { useExportCapabilities } from "@/hooks/useExportCapabilities";
import type { ReportExportFmt } from "@/api/deepResearch";
import type { ScopeId } from "@/shell/mode";
import { useToast } from "@/components/toastApi";
import type { ReportAction, ReportActionName } from "../NeedMoreCard";

interface Params {
  r: ReturnType<typeof useDeepResearch>;
  onScopeChange?: (next: ScopeId) => void;
  draft?: string;
  onDraftChange?: (next: string) => void;
}

export function useDeepResearchSurfaceState({ r, onScopeChange, draft, onDraftChange }: Params) {
  const [localDraft, setLocalDraft] = useState("");
  const draftValue = onDraftChange ? (draft ?? "") : localDraft;
  const setDraftValue = useCallback(
    (next: string) => {
      if (onDraftChange) onDraftChange(next);
      else setLocalDraft(next);
    },
    [onDraftChange],
  );
  // "New research" (top-right, on FINISHED/ERROR) is the sidebar "New" for this
  // surface: clear the current DR session, drop the scope back to Standard, empty
  // the shared draft, and mark every mode fresh so nothing auto-resumes. The <Link
  // to="/"> still navigates when we arrived via /deep/:cid; at "/" the nav is a
  // no-op and this reset is what actually clears the screen (the old bare Link did
  // nothing here — the reported dead button).
  const handleNewResearch = useCallback(() => {
    r.reset();
    onScopeChange?.("standard");
    onDraftChange?.("");
    markAllModesFresh();
  }, [r, onScopeChange, onDraftChange]);
  // Gap #4: publish the DR run status to the W6 E2E bridge (await RUNNING/PAUSED/
  // FINISHED/ERROR); clear on unmount.
  useEffect(() => {
    publishRunStatus(r.status);
    return () => publishRunStatus(null);
  }, [r.status]);
  // RP-07: PDF/DOCX run in the agent-server (weasyprint / pandoc). The buttons are
  // gated on the REAL server capability — never a clickable button that 500s. MD
  // always works; PDF/DOCX enable wherever the server has the toolchain.
  const exportCaps = useExportCapabilities();
  const toast = useToast();
  const doneNotify = useDeepResearchDoneNotification({
    cid: r.cid,
    status: r.status,
    title: r.query,
  });

  // WALK-20: include-follow-ups modal for the TOP-BAR export buttons.
  // When followUps exist, clicking MD/PDF/DOCX in the header opens this modal
  // first; on confirm the real export runs with the selected seqs threaded in.
  const [topBarPendingFmt, setTopBarPendingFmt] = useState<ReportExportFmt | null>(null);
  const [topBarIncludeOpen, setTopBarIncludeOpen] = useState(false);

  const runTopBarExport = useCallback(
    async (fmt: ReportExportFmt, seqs?: number[]) => {
      try {
        await r.exportReportByFmt(fmt, seqs);
      } catch (error) {
        toast.show({
          title: "Export failed",
          body: error instanceof Error ? error.message : "The report could not be exported.",
        });
      }
    },
    [r, toast],
  );

  const handleTopBarExport = useCallback(
    (fmt: ReportExportFmt) => {
      if (r.followUps.length > 0) {
        setTopBarPendingFmt(fmt);
        setTopBarIncludeOpen(true);
      } else {
        void runTopBarExport(fmt);
      }
    },
    [r, runTopBarExport],
  );

  // UI-25: the report actions also live in the top bar. The press is held here
  // because the top bar and the "Need More?" card are siblings; the card runs
  // it, since that is where each action's own state lives.
  const [reportAction, setReportAction] = useState<ReportAction | null>(null);
  const requestReportAction = useCallback((name: ReportActionName) => {
    setReportAction((prev) => ({ name, nonce: (prev?.nonce ?? 0) + 1 }));
  }, []);

  const handleTopBarIncludeConfirm = useCallback(
    (seqs: number[]) => {
      setTopBarIncludeOpen(false);
      if (topBarPendingFmt) {
        void runTopBarExport(topBarPendingFmt, seqs);
      }
      setTopBarPendingFmt(null);
    },
    [topBarPendingFmt, runTopBarExport],
  );

  return {
    draftValue,
    setDraftValue,
    handleNewResearch,
    exportCaps,
    doneNotify,
    topBarIncludeOpen,
    setTopBarIncludeOpen,
    handleTopBarExport,
    handleTopBarIncludeConfirm,
    reportAction,
    requestReportAction,
  };
}
