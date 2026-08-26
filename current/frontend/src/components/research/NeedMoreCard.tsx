/**
 * NeedMoreCard — "Need More?" action card below a finished Deep Research report.
 *
 * Three independent, resettable actions:
 *   1. Ask a Follow-Up  — reveals the ReportFollowUp input in-place (animated)
 *   2. Export as…       — Radix Dialog with MD / PDF choices + File System
 *                         Access API save-picker (graceful fallback for Firefox/Safari)
 *   3. Audio Overview   — state machine (idle → generating → done | unavailable)
 *                         fully wired (needMoreCardParts/AudioSection +
 *                         useAudioOverview) to the agent-server report-audio
 *                         endpoints via api/deepResearch: SSE progress stream
 *                         with a blocking-POST fallback; shows honest error,
 *                         never fake success
 *
 * ROBUSTNESS GUARANTEE: all three actions have INDEPENDENT, RESETTABLE state.
 * No one-way `done` latches. Re-pressing any button re-opens/re-fires.
 * Pressing all three sequentially leaves all controls interactive.
 */

import { useCallback, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  ChevronDown,
  ChevronUp,
  Download,
  Headphones,
  Loader2,
  MessageCircleQuestion,
  Presentation,
} from "lucide-react";
import { startReportDeck } from "@/api/reportDeck";
import { useMode } from "@/shell/mode";
import type { MessageEvent, ReportEvent } from "@/types/agent";
import { ReportFollowUp } from "./ReportFollowUp";
import { IncludeFollowUpsModal } from "./IncludeFollowUpsModal";
import { ACTIVE_BTN, CTRL_BTN } from "./needMoreCardParts/styles";
import { ExportModal } from "./needMoreCardParts/ExportModal";
import { AudioSection } from "./needMoreCardParts/AudioSection";

// ── Main export ───────────────────────────────────────────────────────────────

export interface NeedMoreCardProps {
  /** The finished report (for client-side MD export + audio). */
  report: ReportEvent;
  /** Conversation ID (for server-side PDF export + future audio endpoint). */
  cid: string;
  /** Callback to submit a follow-up question. */
  onFollowUp: (question: string) => void;
  /** True while a follow-up is being submitted. */
  followUpBusy: boolean;
  /** Post-report follow-up Q&A MessageEvents (WALK-20). When non-empty an
   *  "Include follow-ups?" modal is shown before Export / Audio so the user can
   *  choose which pairs to include. Empty list = skip the modal. */
  followUps?: MessageEvent[];
}

export function NeedMoreCard({
  report,
  cid,
  onFollowUp,
  followUpBusy,
  followUps = [],
}: NeedMoreCardProps) {
  // Independent, resettable state per action.
  const [followUpOpen, setFollowUpOpen] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);

  // WALK-20: include-follow-ups modal state for Export and Audio.
  const [includeForExportOpen, setIncludeForExportOpen] = useState(false);
  const [exportFollowUpSeqs, setExportFollowUpSeqs] = useState<number[]>([]);
  const [includeForAudioOpen, setIncludeForAudioOpen] = useState(false);
  const [audioFollowUpSeqs, setAudioFollowUpSeqs] = useState<number[]>([]);
  const [audioModeOpen, setAudioModeOpen] = useState(false);

  const hasFollowUps = followUps.length > 0;
  const navigate = useNavigate();
  const { setMode } = useMode();
  const [building, setBuilding] = useState(false);

  const toggleFollowUp = useCallback(() => setFollowUpOpen((v) => !v), []);

  // Start the typed server-owned report → deck job. The server resolves the
  // authoritative latest ReportEvent; the client sends no report prose or
  // client-authored authority. On failure we stay on the card so the user can retry.
  const handleBuildDeck = useCallback(async () => {
    if (building) return;
    setBuilding(true);
    try {
      const job = await startReportDeck(cid);
      // Agent is the user-facing task surface. The server independently pins
      // this conversation to artifact mode and the sealed deck contract.
      setMode("agent");
      navigate(`/agent/${job.conversation_id}`);
    } catch {
      setBuilding(false); // surface stays; the button re-enables for a retry
    }
  }, [building, cid, navigate, setMode]);

  // Export button: show include-modal first if follow-ups exist.
  const handleExportClick = useCallback(() => {
    if (hasFollowUps) {
      setIncludeForExportOpen(true);
    } else {
      setExportOpen(true);
    }
  }, [hasFollowUps]);

  const handleExportIncludeConfirm = useCallback((seqs: number[]) => {
    setExportFollowUpSeqs(seqs);
    setIncludeForExportOpen(false);
    setExportOpen(true);
  }, []);

  // Audio button: show include-modal first if follow-ups exist.
  const handleAudioClick = useCallback(() => {
    if (hasFollowUps) {
      setIncludeForAudioOpen(true);
    } else {
      setAudioModeOpen(true);
    }
  }, [hasFollowUps]);

  const handleAudioIncludeConfirm = useCallback((seqs: number[]) => {
    setAudioFollowUpSeqs(seqs);
    setIncludeForAudioOpen(false);
    setAudioModeOpen(true);
  }, []);

  return (
    <section
      aria-label="Need More?"
      className="rounded-card border border-hairline bg-surface-1 p-body"
    >
      <h3 className="mb-section font-display text-[1.1rem] font-medium text-text">
        Need More?
      </h3>

      {/* Three independent action buttons */}
      <div className="flex flex-wrap items-center gap-inline">
        {/* 1. Ask a Follow-Up — toggles the inline follow-up input */}
        <button
          type="button"
          onClick={toggleFollowUp}
          aria-expanded={followUpOpen}
          aria-controls="need-more-follow-up"
          className={followUpOpen ? ACTIVE_BTN : CTRL_BTN}
        >
          <MessageCircleQuestion className="size-3.5" aria-hidden />
          Ask a Follow-Up
          {followUpOpen ? (
            <ChevronUp className="size-3" aria-hidden />
          ) : (
            <ChevronDown className="size-3" aria-hidden />
          )}
        </button>

        {/* 2. Export as… — opens include-modal first if follow-ups exist */}
        <button
          type="button"
          onClick={handleExportClick}
          className={CTRL_BTN}
        >
          <Download className="size-3.5" aria-hidden />
          Export as…
        </button>

        {/* 3. Audio Overview — opens include-modal first if follow-ups exist */}
        <button
          type="button"
          onClick={handleAudioClick}
          className={CTRL_BTN}
        >
          <Headphones className="size-3.5" aria-hidden />
          Audio Overview
        </button>

        {/* 4. Build a deck — autonomous handoff: turn this report into slides */}
        <button
          type="button"
          onClick={handleBuildDeck}
          disabled={building}
          data-disco-control="dr.build-deck"
          className={CTRL_BTN}
        >
          {building ? (
            <Loader2 className="size-3.5 animate-spin" aria-hidden />
          ) : (
            <Presentation className="size-3.5" aria-hidden />
          )}
          {building ? "Starting…" : "Build a deck"}
        </button>
        <AudioSection
          cid={cid}
          followUpSeqs={audioFollowUpSeqs}
          modeOpen={audioModeOpen}
          onModeOpenChange={setAudioModeOpen}
          reportQuery={report.query}
        />
      </div>

      {/* Inline follow-up panel (pmx-rise on mount) */}
      {followUpOpen && (
        <div
          id="need-more-follow-up"
          className="mt-section pmx-rise"
          role="region"
          aria-label="Follow-up input"
        >
          <ReportFollowUp onAsk={onFollowUp} busy={followUpBusy} />
        </div>
      )}

      {/* WALK-20: include-follow-ups modal for Export */}
      <IncludeFollowUpsModal
        open={includeForExportOpen}
        onOpenChange={setIncludeForExportOpen}
        followUps={followUps}
        onConfirm={handleExportIncludeConfirm}
        actionLabel="Export"
      />

      {/* WALK-20: include-follow-ups modal for Audio */}
      <IncludeFollowUpsModal
        open={includeForAudioOpen}
        onOpenChange={setIncludeForAudioOpen}
        followUps={followUps}
        onConfirm={handleAudioIncludeConfirm}
        actionLabel="Generate audio"
      />

      {/* Export modal (Radix Dialog) */}
      <ExportModal
        open={exportOpen}
        onOpenChange={setExportOpen}
        report={report}
        cid={cid}
        followUpSeqs={exportFollowUpSeqs.length > 0 ? exportFollowUpSeqs : undefined}
      />
    </section>
  );
}
