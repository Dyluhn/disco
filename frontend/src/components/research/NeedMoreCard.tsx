/**
 * NeedMoreCard — "Need More?" action card below a finished Deep Research report.
 *
 * Three independent, resettable actions:
 *   1. Ask a Follow-Up  — reveals the ReportFollowUp input in-place (animated)
 *   2. Export as…       — Radix Dialog with MD / PDF / DOCX choices + File System
 *                         Access API save-picker (graceful fallback for Firefox/Safari)
 *   3. Audio Overview   — state machine (idle → generating → done | unavailable)
 *                         wired to a TODO stub; shows honest error, never fake success
 *
 * ROBUSTNESS GUARANTEE: all three actions have INDEPENDENT, RESETTABLE state.
 * No one-way `done` latches. Re-pressing any button re-opens/re-fires.
 * Pressing all three sequentially leaves all controls interactive.
 */

import * as Dialog from "@radix-ui/react-dialog";
import { useCallback, useState } from "react";
import {
  ArrowDown,
  ChevronDown,
  ChevronUp,
  Download,
  File as FileIcon,
  FileText,
  FileType,
  Headphones,
  Loader2,
  MessageCircleQuestion,
  Play,
  X,
} from "lucide-react";
import { cn } from "@/lib/cn";
import {
  exportReport,
  exportReportAsMarkdown,
  requestReportAudio,
  serializeReportToMarkdown,
} from "@/api/deepResearch";
import { agentHttpBase } from "@/api/client";
import { useExportCapabilities } from "@/hooks/useExportCapabilities";
import type { ReportEvent } from "@/types/agent";
import { ReportFollowUp } from "./ReportFollowUp";

// ── Shared button tokens (match CTRL_BTN in DeepResearchSurface.tsx) ─────────

const CTRL_BTN =
  "flex items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:text-text";

const ACTIVE_BTN =
  "flex items-center gap-hair rounded-control border border-accent/60 bg-accent/5 px-inline py-hair font-ui text-[0.78rem] text-accent transition-colors hover:border-accent";

// ── Audio state machine ───────────────────────────────────────────────────────

type AudioState =
  | { status: "idle" }
  | { status: "generating"; progress: number }
  | { status: "done"; audioUrl: string }
  | { status: "unavailable"; reason: string };

// Audio overview is wired to the agent-server endpoint
// POST /conversations/{cid}/report/audio (see api/deepResearch.requestReportAudio).
// On failure (e.g. TTS disabled in Settings) it surfaces the typed reason honestly
// — never a fake success.

// ── File System Access API types ──────────────────────────────────────────────

interface SaveFilePickerOptions {
  suggestedName?: string;
  types?: Array<{ description?: string; accept: Record<string, string[]> }>;
}

interface FSAWindow {
  showSaveFilePicker: (opts?: SaveFilePickerOptions) => Promise<FileSystemFileHandle>;
}

function hasFSA(): boolean {
  return (
    typeof window !== "undefined" &&
    "showSaveFilePicker" in window
  );
}

async function saveViaPicker(
  blob: Blob,
  opts: SaveFilePickerOptions,
): Promise<void> {
  const win = window as unknown as FSAWindow;
  const fh = await win.showSaveFilePicker(opts);
  const writable = await fh.createWritable();
  await writable.write(blob);
  await writable.close();
}

// ── Export modal ──────────────────────────────────────────────────────────────

interface ExportModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  report: ReportEvent;
  cid: string;
}

function ExportModal({ open, onOpenChange, report, cid }: ExportModalProps) {
  const exportCaps = useExportCapabilities();
  const [exporting, setExporting] = useState<"md" | "pdf" | "docx" | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const fsa = hasFSA();

  const handleMd = useCallback(async () => {
    setExporting("md");
    setExportError(null);
    try {
      if (fsa) {
        const md = serializeReportToMarkdown(report);
        const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
        await saveViaPicker(blob, {
          suggestedName: sanitizeFilename(report.query) + ".md",
          types: [{ description: "Markdown", accept: { "text/markdown": [".md"] } }],
        });
      } else {
        exportReportAsMarkdown(report);
      }
    } catch (e: unknown) {
      // User cancelled the save picker — not an error condition
      if (!(e instanceof DOMException && e.name === "AbortError")) {
        setExportError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setExporting(null);
    }
  }, [report, fsa]);

  const handleFmt = useCallback(
    async (fmt: "pdf" | "docx") => {
      setExporting(fmt);
      setExportError(null);
      try {
        if (fsa) {
          // Fetch the blob from the server so we can pass it to showSaveFilePicker
          const res = await fetch(
            `/api/conversations/${cid}/report/export?fmt=${fmt}`,
            { method: "POST" },
          );
          if (!res.ok) {
            let detail = `${res.status}`;
            try {
              const body = await res.json();
              detail = body.detail?.reason || body.detail || JSON.stringify(body);
            } catch {
              // not JSON
            }
            throw new Error(`Export failed (${res.status}): ${detail}`);
          }
          const blob = await res.blob();
          const ext = fmt === "pdf" ? ".pdf" : ".docx";
          const mimeType =
            fmt === "pdf"
              ? "application/pdf"
              : "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
          await saveViaPicker(blob, {
            suggestedName: sanitizeFilename(report.query) + ext,
            types: [{ description: fmt.toUpperCase(), accept: { [mimeType]: [ext] } }],
          });
        } else {
          await exportReport(cid, fmt);
        }
      } catch (e: unknown) {
        if (!(e instanceof DOMException && e.name === "AbortError")) {
          setExportError(e instanceof Error ? e.message : String(e));
        }
      } finally {
        setExporting(null);
      }
    },
    [cid, fsa, report.query],
  );

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/30 backdrop-blur-sm" />
        <Dialog.Content
          className={cn(
            "fixed left-1/2 top-1/2 z-50 w-[min(26rem,92vw)]",
            "-translate-x-1/2 -translate-y-1/2",
            "bg-[color-mix(in_oklch,var(--surface-1)_80%,transparent)]",
            "backdrop-blur-md border border-hairline rounded-card p-body pmx-rise",
          )}
          aria-labelledby="export-modal-title"
        >
          <div className="flex items-center justify-between">
            <Dialog.Title
              id="export-modal-title"
              className="font-ui text-[0.95rem] font-semibold text-text"
            >
              Export report
            </Dialog.Title>
            <Dialog.Close asChild>
              <button
                type="button"
                aria-label="Close export dialog"
                className="rounded-control p-hair text-text-faint transition-colors hover:text-text"
              >
                <X className="size-4" aria-hidden />
              </button>
            </Dialog.Close>
          </div>

          <Dialog.Description className="mt-hair font-ui text-[0.82rem] text-text-muted">
            Choose a format.{" "}
            {fsa
              ? "A save picker lets you choose where to put the file."
              : "The file downloads to your default Downloads folder."}
          </Dialog.Description>

          <div className="mt-section flex flex-col gap-inline">
            {/* MD — always available */}
            <button
              type="button"
              onClick={handleMd}
              disabled={exporting !== null}
              className={cn(
                "flex items-center gap-inline rounded-control border border-hairline p-inline",
                "font-ui text-[0.85rem] text-text-muted transition-colors text-left",
                "hover:border-accent hover:text-accent",
                exporting === "md" && "opacity-60 cursor-wait",
                exporting !== null && exporting !== "md" && "opacity-40",
              )}
            >
              <FileText className="size-4 shrink-0 text-accent" aria-hidden />
              <div className="flex-1">
                <div className="font-medium text-text">Markdown</div>
                <div className="text-[0.74rem] text-text-faint">Portable plain-text</div>
              </div>
              {exporting === "md" ? (
                <Loader2 className="size-4 animate-spin text-text-faint" aria-hidden />
              ) : (
                <ArrowDown className="size-3.5 text-text-faint" aria-hidden />
              )}
            </button>

            {/* PDF — gated on server capability */}
            <button
              type="button"
              onClick={() => handleFmt("pdf")}
              disabled={!exportCaps.pdf || exporting !== null}
              aria-disabled={!exportCaps.pdf}
              title={
                exportCaps.pdf
                  ? "Download as PDF"
                  : "PDF export requires WeasyPrint on the server"
              }
              className={cn(
                "flex items-center gap-inline rounded-control border p-inline",
                "font-ui text-[0.85rem] transition-colors text-left",
                exportCaps.pdf
                  ? "border-hairline text-text-muted hover:border-accent hover:text-accent"
                  : "border-dashed border-hairline text-text-faint opacity-50 cursor-not-allowed",
                exporting === "pdf" && "opacity-60 cursor-wait",
                exporting !== null && exporting !== "pdf" && exportCaps.pdf && "opacity-40",
              )}
            >
              <FileType
                className={cn("size-4 shrink-0", exportCaps.pdf ? "text-accent" : "text-text-faint")}
                aria-hidden
              />
              <div className="flex-1">
                <div className={cn("font-medium", exportCaps.pdf ? "text-text" : "text-text-faint")}>
                  PDF
                </div>
                <div className="text-[0.74rem] text-text-faint">
                  {exportCaps.pdf ? "Print-ready PDF" : "Needs WeasyPrint on the server"}
                </div>
              </div>
              {exporting === "pdf" ? (
                <Loader2 className="size-4 animate-spin text-text-faint" aria-hidden />
              ) : (
                <ArrowDown
                  className={cn("size-3.5", exportCaps.pdf ? "text-text-faint" : "opacity-30 text-text-faint")}
                  aria-hidden
                />
              )}
            </button>

            {/* DOCX — gated on server capability */}
            <button
              type="button"
              onClick={() => handleFmt("docx")}
              disabled={!exportCaps.docx || exporting !== null}
              aria-disabled={!exportCaps.docx}
              title={
                exportCaps.docx
                  ? "Download as DOCX"
                  : "DOCX export requires pandoc on the server"
              }
              className={cn(
                "flex items-center gap-inline rounded-control border p-inline",
                "font-ui text-[0.85rem] transition-colors text-left",
                exportCaps.docx
                  ? "border-hairline text-text-muted hover:border-accent hover:text-accent"
                  : "border-dashed border-hairline text-text-faint opacity-50 cursor-not-allowed",
                exporting === "docx" && "opacity-60 cursor-wait",
                exporting !== null && exporting !== "docx" && exportCaps.docx && "opacity-40",
              )}
            >
              <FileIcon
                className={cn("size-4 shrink-0", exportCaps.docx ? "text-accent" : "text-text-faint")}
                aria-hidden
              />
              <div className="flex-1">
                <div className={cn("font-medium", exportCaps.docx ? "text-text" : "text-text-faint")}>
                  DOCX
                </div>
                <div className="text-[0.74rem] text-text-faint">
                  {exportCaps.docx
                    ? "Word-compatible document"
                    : "Needs pandoc on the server"}
                </div>
              </div>
              {exporting === "docx" ? (
                <Loader2 className="size-4 animate-spin text-text-faint" aria-hidden />
              ) : (
                <ArrowDown
                  className={cn("size-3.5", exportCaps.docx ? "text-text-faint" : "opacity-30 text-text-faint")}
                  aria-hidden
                />
              )}
            </button>
          </div>

          {exportError && (
            <p role="alert" className="mt-inline font-ui text-[0.78rem] text-unsupported">
              {exportError}
            </p>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

// ── Audio section ─────────────────────────────────────────────────────────────

interface AudioSectionProps {
  cid: string;
}

function AudioSection({ cid }: AudioSectionProps) {
  const [audio, setAudio] = useState<AudioState>({ status: "idle" });

  const generate = useCallback(async () => {
    setAudio({ status: "generating", progress: 0 });
    try {
      const { mp3_url } = await requestReportAudio(cid);
      // The endpoint returns an agent-server-relative URL; prefix the agent base
      // so the <audio> element + Export fetch the right origin.
      setAudio({ status: "done", audioUrl: `${agentHttpBase()}${mp3_url}` });
    } catch (e: unknown) {
      const reason = e instanceof Error ? e.message : String(e);
      setAudio({ status: "unavailable", reason });
    }
  }, [cid]);

  const reset = useCallback(() => setAudio({ status: "idle" }), []);

  if (audio.status === "idle") {
    return (
      <button type="button" onClick={generate} className={CTRL_BTN}>
        <Headphones className="size-3.5" aria-hidden />
        Audio Overview
      </button>
    );
  }

  if (audio.status === "generating") {
    return (
      <div className="flex items-center gap-hair">
        <Loader2 className="size-3.5 animate-spin text-text-faint" aria-hidden />
        <span className="font-ui text-[0.78rem] text-text-muted">
          Generating audio{audio.progress > 0 ? ` ${audio.progress}%` : "…"}
        </span>
      </div>
    );
  }

  if (audio.status === "done") {
    // Real Play + Export affordances — gated on a real Blob, never a fake.
    const downloadMp3 = () => {
      const a = document.createElement("a");
      a.href = audio.audioUrl;
      a.download = "audio-overview.mp3";
      document.body.appendChild(a);
      a.click();
      a.remove();
    };

    return (
      <div className="flex items-center gap-inline">
        <audio src={audio.audioUrl} controls className="h-8 max-w-[16rem]" />
        <button type="button" onClick={downloadMp3} className={CTRL_BTN}>
          <ArrowDown className="size-3.5" aria-hidden />
          Export MP3
        </button>
        <button type="button" onClick={reset} className={CTRL_BTN}>
          <Play className="size-3.5" aria-hidden />
          Regenerate
        </button>
      </div>
    );
  }

  // "unavailable" — honest error, NO false success, resettable
  return (
    <div className="flex flex-col gap-hair">
      <div className="flex flex-wrap items-center gap-inline">
        <Headphones className="size-3.5 shrink-0 text-text-faint" aria-hidden />
        <span className="font-ui text-[0.78rem] text-text-faint">
          {audio.status === "unavailable" ? audio.reason : "Audio overview unavailable"}
        </span>
        <button
          type="button"
          onClick={reset}
          aria-label="Reset audio overview state"
          className={CTRL_BTN}
        >
          Try again
        </button>
      </div>
      {/* fix-c #7: the audio endpoint is real now — the previous "(stub: …)"
          note was stale and dishonest. The typed reason above is the honest
          signal; no second line needed. */}
    </div>
  );
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function sanitizeFilename(name: string): string {
  return (
    (name || "research-report")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 60) || "research-report"
  );
}

// ── Main export ───────────────────────────────────────────────────────────────

export interface NeedMoreCardProps {
  /** The finished report (for client-side MD export + audio). */
  report: ReportEvent;
  /** Conversation ID (for server-side PDF/DOCX export + future audio endpoint). */
  cid: string;
  /** Callback to submit a follow-up question. */
  onFollowUp: (question: string) => void;
  /** True while a follow-up is being submitted. */
  followUpBusy: boolean;
}

export function NeedMoreCard({
  report,
  cid,
  onFollowUp,
  followUpBusy,
}: NeedMoreCardProps) {
  // Independent, resettable state per action.
  const [followUpOpen, setFollowUpOpen] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);

  const toggleFollowUp = useCallback(() => setFollowUpOpen((v) => !v), []);

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

        {/* 2. Export as… — opens the modal */}
        <button
          type="button"
          onClick={() => setExportOpen(true)}
          className={CTRL_BTN}
        >
          <Download className="size-3.5" aria-hidden />
          Export as…
        </button>

        {/* 3. Audio Overview — state machine */}
        <AudioSection cid={cid} />
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

      {/* Export modal (Radix Dialog) */}
      <ExportModal
        open={exportOpen}
        onOpenChange={setExportOpen}
        report={report}
        cid={cid}
      />
    </section>
  );
}
