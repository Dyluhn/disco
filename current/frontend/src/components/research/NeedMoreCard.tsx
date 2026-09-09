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
  Loader2,
  MessageCircleQuestion,
  Presentation,
} from "lucide-react";
import { readVerboseAgentChat } from "@/lib/useVerboseAgentChat";
import { createBuildConversation } from "@/api/agent";
import { serializeReportToMarkdown } from "@/api/deepResearch";
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

  // A5: hand the finished report off to an AUTONOMOUS build that turns it into a slide
  // deck. We create the build conversation (autonomous=true → the deck plan auto-
  // approves for a frictionless "just build it"; per-action risk gates still apply),
  // then navigate to it with the serialized report as the seed task — BuildSurface
  // kicks it once. On failure we stay on the card so the user can retry (no dead end).
  const handleBuildDeck = useCallback(async () => {
    if (building) return;
    setBuilding(true);
    try {
      // R3: keep the VISIBLE handoff message short (a one-liner the chat history
      // shows), and pass the full report as hidden `seedContext` — stored as an
      // ENVIRONMENT message the model receives but the user doesn't see as a
      // screen-filling bubble. The "Deep research report …" framing also guarantees
      // the hidden message can't be mistaken for a ⚠/upload notice (which would surface).
      const q = (report.query || "").trim();
      const seedTask = q
        ? `Make slides for the deep research report: "${q}"`
        : "Make slides for the deep research report.";
      const seedContext =
        "Deep research report to turn into a polished slide deck " +
        "(use the slides_generate tool):\n\n" +
        serializeReportToMarkdown(report);
      // runthru-v2 #7: route slide-making to the AGENT surface (task framing, not the
      // "software developer"/live-preview build framing). W-13: run it AUTONOMOUS
      // (autonomous=true) so the seeded slides plan auto-approves for a frictionless
      // "just build the deck" handoff — per-action risk gates still apply.
      // model_override=null → server uses the last-selected pick.
      const newCid = await createBuildConversation(
        null,
        "agent",
        true,
        null,
        !readVerboseAgentChat(),
      );
      // W-24: this handoff lands on the AGENT surface (/agent/:cid). Sync the
      // 3-way mode slider to "agent" so it reflects where we just navigated —
      // otherwise the slider stayed on Search while the Agent surface rendered.
      setMode("agent");
      navigate(`/agent/${newCid}`, { state: { seedTask, seedContext } });
    } catch {
      setBuilding(false); // surface stays; the button re-enables for a retry
    }
  }, [building, report, navigate, setMode]);

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

        {/* 3. Audio Overview — the trigger, the progress row, the player and
            the Export/Regenerate controls are all AudioSection's, so the
            trigger can disappear once a player exists (UI-23). */}
        <AudioSection
          cid={cid}
          followUpSeqs={audioFollowUpSeqs}
          modeOpen={audioModeOpen}
          onModeOpenChange={setAudioModeOpen}
          reportQuery={report.query}
          onRequestGenerate={handleAudioClick}
        />

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
