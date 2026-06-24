/**
 * NeedMoreCard — "Need More?" action card below a finished Deep Research report.
 *
 * Three independent, resettable actions:
 *   1. Ask a Follow-Up  — reveals the ReportFollowUp input in-place (animated)
 *   2. Export as…       — Radix Dialog with MD / PDF choices + File System
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
import { useNavigate } from "react-router-dom";
import {
  ArrowDown,
  ChevronDown,
  ChevronUp,
  Download,
  FileText,
  FileType,
  Headphones,
  Loader2,
  MessageCircleQuestion,
  Mic,
  Presentation,
  Radio,
  X,
} from "lucide-react";
import { cn } from "@/lib/cn";
// B1/B4: MD and PDF non-FSA paths now use inline fetch+blob-URL
// so we no longer call exportReport / exportReportAsMarkdown from deepResearch.ts.
import { agentHttpBase } from "@/api/client";
import { createBuildConversation } from "@/api/agent";
import { serializeReportToMarkdown } from "@/api/deepResearch";
import { useExportCapabilities } from "@/hooks/useExportCapabilities";
import { useTemplates } from "@/hooks/useTemplates";
import { useMode } from "@/shell/mode";
import { TemplatePicker } from "./TemplatePicker";
import type { MessageEvent, ReportEvent } from "@/types/agent";
import { ReportFollowUp } from "./ReportFollowUp";
import { AudioPlayer } from "./AudioPlayer";
import { IncludeFollowUpsModal } from "./IncludeFollowUpsModal";

// ── Shared button tokens (match CTRL_BTN in DeepResearchSurface.tsx) ─────────

const CTRL_BTN =
  "flex items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:text-text";

const ACTIVE_BTN =
  "flex items-center gap-hair rounded-control border border-accent/60 bg-accent/5 px-inline py-hair font-ui text-[0.78rem] text-accent transition-colors hover:border-accent";

// ── Audio types ───────────────────────────────────────────────────────────────

type AudioMode = "podcast" | "single";

// W-08 / W-09: real pipeline stages streamed from the server (SSE). The UI keys
// its status copy on these so the "downloading voice model…" note ONLY shows on
// a genuine first-run download — never on a warm cache or remote backend.
type AudioStage =
  | "preparing"
  | "downloading_model"
  | "synthesizing"
  | "mixing"
  | "cache_hit";

interface AudioProgress {
  stage: AudioStage;
  current?: number;
  total?: number;
  // W-08: REAL voice-model download byte counters (only on `downloading_model`).
  downloaded?: number;
  bytesTotal?: number;
  pct?: number;
  fileIndex?: number;
  fileTotal?: number;
}

type AudioState =
  | { status: "idle" }
  | { status: "generating"; progress?: AudioProgress }
  | { status: "done"; audioUrl: string }
  | { status: "unavailable"; reason: string };

// Audio overview calls the agent-server endpoint directly so that the `mode`
// query param can be threaded through without touching the shared deepResearch.ts
// API file (owned by another lane).  The mode is chosen via a popup dialog before
// generation starts (WALK-21 / D3).

/** Map a server error reason to a human message. */
function audioReasonMessage(reason: string): string {
  if (reason === "tts_disabled") {
    return "Audio overview is disabled in Settings → Audio — enable it to generate.";
  }
  return `Audio overview failed: ${reason}`;
}

/** Human-readable label for the current generation stage (W-09). */
function audioStageLabel(progress: AudioProgress | undefined): string {
  if (!progress) return "Generating audio…";
  switch (progress.stage) {
    case "preparing":
      return "Preparing script…";
    case "downloading_model": {
      const part =
        progress.fileTotal && progress.fileTotal > 1
          ? ` (file ${progress.fileIndex ?? 1}/${progress.fileTotal})`
          : "";
      return progress.pct != null
        ? `Downloading voice model${part}… ${progress.pct}%`
        : "Downloading voice model…";
    }
    case "synthesizing":
      return progress.total
        ? `Synthesizing turns ${progress.current ?? 0}/${progress.total}…`
        : "Synthesizing audio…";
    case "mixing":
      return "Mixing audio…";
    case "cache_hit":
      return "Loading cached audio…";
    default:
      return "Generating audio…";
  }
}

/** Bytes → a compact "12.3 MB" label for the download progress note. */
function fmtMB(bytes: number | undefined): string {
  if (!bytes || bytes <= 0) return "0 MB";
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Determinate fill 0–100 for the current generation stage, or null when the
 * stage has no measurable progress (indeterminate spinner only). */
function audioProgressPct(progress: AudioProgress | undefined): number | null {
  if (!progress) return null;
  if (progress.stage === "downloading_model" && progress.pct != null) {
    return Math.max(0, Math.min(100, progress.pct));
  }
  if (progress.stage === "synthesizing" && progress.total) {
    return Math.max(0, Math.min(100, ((progress.current ?? 0) / progress.total) * 100));
  }
  return null;
}

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
  /** Selected follow-up user-message seqs to include (WALK-20). */
  followUpSeqs?: number[];
}

function ExportModal({ open, onOpenChange, report, cid, followUpSeqs }: ExportModalProps) {
  const exportCaps = useExportCapabilities();
  const [exporting, setExporting] = useState<"md" | "pdf" | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  // Export template — a brand theme spin. Threaded into the POST body as `theme`
  // (registry name) + `mode` (split from the chosen catalogue entry). Default =
  // Disco light; MD ignores it (byte-identical regardless).
  const templates = useTemplates();
  const [templateId, setTemplateId] = useState("disco-light");
  const tpl = templates.find((t) => t.id === templateId) ?? templates[0];
  const fsa = hasFSA();

  const hasFollowUps = Boolean(followUpSeqs && followUpSeqs.length > 0);

  // Shared helper: fetch export blob from server and trigger a download.
  // Used by both FSA and non-FSA paths so all export types get titles.
  async function _fetchExportBlob(
    fmt: "md" | "pdf",
    bodyPayload: string | undefined,
  ): Promise<Blob> {
    const res = await fetch(
      `${agentHttpBase()}/api/conversations/${cid}/report/export?fmt=${fmt}`,
      {
        method: "POST",
        headers: bodyPayload ? { "Content-Type": "application/json" } : undefined,
        body: bodyPayload,
      },
    );
    if (!res.ok) {
      let detail = `${res.status}`;
      try {
        const body = await res.json();
        detail = body.detail?.reason || body.detail || JSON.stringify(body);
      } catch {
        /* not JSON */
      }
      throw new Error(`Export failed (${res.status}): ${detail}`);
    }
    return res.blob();
  }

  // B4: derive filename from report title, not conv_id.
  const baseFilename = sanitizeFilename(report.query);

  const handleMd = useCallback(async () => {
    setExporting("md");
    setExportError(null);
    // B1: always route through the server endpoint so follow-ups are included
    // regardless of FSA availability.  (The server endpoint is idempotent —
    // no extra cost over the client-side serializer.)
    // Include theme+mode so PDF carries the chosen template; server ignores for MD.
    const exportBody: { follow_up_seqs?: number[]; theme: string; mode: string } = {
      theme: tpl.name,
      mode: tpl.mode,
    };
    if (hasFollowUps) exportBody.follow_up_seqs = followUpSeqs;
    const bodyPayload = JSON.stringify(exportBody);
    try {
      const blob = await _fetchExportBlob("md", bodyPayload);
      if (fsa) {
        await saveViaPicker(blob, {
          suggestedName: baseFilename + ".md",
          types: [{ description: "Markdown", accept: { "text/markdown": [".md"] } }],
        });
      } else {
        // B4: use report title as filename.
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = baseFilename + ".md";
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
      }
    } catch (e: unknown) {
      if (!(e instanceof DOMException && e.name === "AbortError")) {
        setExportError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setExporting(null);
    }
  }, [report.query, cid, fsa, hasFollowUps, followUpSeqs, baseFilename, tpl]);

  const handleFmt = useCallback(
    async (fmt: "pdf") => {
      setExporting(fmt);
      setExportError(null);
      // Thread theme+mode so PDF honours the chosen template.
      const exportBody: { follow_up_seqs?: number[]; theme: string; mode: string } = {
        theme: tpl.name,
        mode: tpl.mode,
      };
      if (hasFollowUps) exportBody.follow_up_seqs = followUpSeqs;
      const bodyPayload = JSON.stringify(exportBody);

      try {
        const ext = ".pdf";
        const mimeType = "application/pdf";
        const blob = await _fetchExportBlob(fmt, bodyPayload);
        if (fsa) {
          await saveViaPicker(blob, {
            suggestedName: baseFilename + ext,
            types: [{ description: fmt.toUpperCase(), accept: { [mimeType]: [ext] } }],
          });
        } else {
          // B4: use report title for non-FSA download too.
          const url = URL.createObjectURL(blob);
          const a = document.createElement("a");
          a.href = url;
          a.download = baseFilename + ext;
          document.body.appendChild(a);
          a.click();
          a.remove();
          URL.revokeObjectURL(url);
        }
      } catch (e: unknown) {
        if (!(e instanceof DOMException && e.name === "AbortError")) {
          setExportError(e instanceof Error ? e.message : String(e));
        }
      } finally {
        setExporting(null);
      }
    },
    [cid, fsa, hasFollowUps, followUpSeqs, baseFilename, tpl],
  );

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/40 backdrop-blur-md" />
        <Dialog.Content
          className={cn(
            "fixed left-1/2 top-1/2 z-50 w-[min(26rem,92vw)]",
            "-translate-x-1/2 -translate-y-1/2",
            "bg-[color-mix(in_oklch,var(--surface-1)_95%,transparent)]",
            "backdrop-blur-xl border border-hairline rounded-card p-body pmx-rise",
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
              data-disco-control="dr.export.modal.md"
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
              data-disco-control="dr.export.modal.pdf"
              data-export-cap={String(exportCaps.pdf)}
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

            {/* PDF template — the brand-theme spin, only where PDF export is real */}
            {exportCaps.pdf && (
              <TemplatePicker
                templates={templates}
                value={templateId}
                onChange={setTemplateId}
                disabled={exporting !== null}
                label="PDF template"
                id="pdf-template"
                dataControl="dr.export-template"
              />
            )}
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

// ── Audio mode chooser dialog ─────────────────────────────────────────────────

interface AudioModeDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onChoose: (mode: AudioMode) => void;
}

function AudioModeDialog({ open, onOpenChange, onChoose }: AudioModeDialogProps) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/40 backdrop-blur-md" />
        <Dialog.Content
          className={cn(
            "fixed left-1/2 top-1/2 z-50 w-[min(26rem,92vw)]",
            "-translate-x-1/2 -translate-y-1/2",
            "bg-[color-mix(in_oklch,var(--surface-1)_95%,transparent)]",
            "backdrop-blur-xl border border-hairline rounded-card p-body pmx-rise",
          )}
          aria-labelledby="audio-mode-title"
        >
          <div className="flex items-center justify-between">
            <Dialog.Title
              id="audio-mode-title"
              className="font-ui text-[0.95rem] font-semibold text-text"
            >
              Generate audio overview
            </Dialog.Title>
            <Dialog.Close asChild>
              <button
                type="button"
                aria-label="Close audio mode dialog"
                className="rounded-control p-hair text-text-faint transition-colors hover:text-text"
              >
                <X className="size-4" aria-hidden />
              </button>
            </Dialog.Close>
          </div>

          <Dialog.Description className="mt-hair font-ui text-[0.82rem] text-text-muted">
            Choose an audio style for this research report.
          </Dialog.Description>

          <div className="mt-section flex flex-col gap-inline">
            {/* Podcast style */}
            <button
              type="button"
              onClick={() => onChoose("podcast")}
              data-disco-control="dr.audio.podcast"
              className={cn(
                "flex items-start gap-inline rounded-card border border-hairline p-inline",
                "font-ui text-[0.85rem] text-text-muted transition-colors text-left",
                "hover:border-accent hover:text-accent",
              )}
            >
              <Radio className="mt-px size-4 shrink-0 text-accent" aria-hidden />
              <div className="flex-1">
                <div className="flex items-center gap-hair font-medium text-text">
                  Podcast style
                  <span className="rounded border border-accent/40 bg-accent/10 px-[0.3rem] py-px font-ui text-[0.65rem] font-semibold uppercase tracking-wide text-accent">
                    alpha
                  </span>
                </div>
                <div className="text-[0.74rem] text-text-faint">
                  Two hosts discuss the key findings in a conversational back-and-forth.
                </div>
              </div>
            </button>

            {/* Single speaker */}
            <button
              type="button"
              onClick={() => onChoose("single")}
              data-disco-control="dr.audio.single"
              className={cn(
                "flex items-start gap-inline rounded-card border border-hairline p-inline",
                "font-ui text-[0.85rem] text-text-muted transition-colors text-left",
                "hover:border-accent hover:text-accent",
              )}
            >
              <Mic className="mt-px size-4 shrink-0 text-accent" aria-hidden />
              <div className="flex-1">
                <div className="font-medium text-text">Single speaker</div>
                <div className="text-[0.74rem] text-text-faint">
                  An honest walkthrough — covers the findings, discusses gaps, and gives a
                  balanced breakdown. Uses the Host A voice from Settings.
                </div>
              </div>
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

// ── Audio section ─────────────────────────────────────────────────────────────

interface AudioSectionProps {
  cid: string;
  /** Selected follow-up seqs to include in the audio (WALK-20). */
  followUpSeqs?: number[];
  /** Controlled: whether the mode-pick dialog is open (parent drives this
   *  when it needs to insert the include-follow-ups step first). */
  modeOpen: boolean;
  onModeOpenChange: (open: boolean) => void;
  /** B4: report title/query for a meaningful download filename. */
  reportQuery: string;
}

function AudioSection({
  cid,
  followUpSeqs,
  modeOpen,
  onModeOpenChange,
  reportQuery,
}: AudioSectionProps) {
  const [audio, setAudio] = useState<AudioState>({ status: "idle" });

  // Called after the user picks a mode in the dialog.
  // Calls the endpoint directly (not via requestReportAudio from deepResearch.ts)
  // so we can thread the ?mode= query param + JSON body without editing that file.
  //
  // W-09: prefer the SSE streaming endpoint so the UI shows REAL staged progress
  // ("Synthesizing turns 3/8…", "Mixing…"). The blocking POST is kept as a
  // fallback (W-08/W-09 robustness): if streaming can't start or yields no
  // terminal event, we re-issue the blocking request.
  const generate = useCallback(
    async (mode: AudioMode) => {
      onModeOpenChange(false);
      setAudio({ status: "generating" });

      const bodyPayload =
        followUpSeqs && followUpSeqs.length > 0
          ? JSON.stringify({ follow_up_seqs: followUpSeqs })
          : undefined;
      const headers = bodyPayload
        ? { "Content-Type": "application/json" }
        : undefined;

      // Returns true once a terminal (done / error) event was handled, false to
      // fall back to the blocking endpoint.
      const viaStream = async (): Promise<boolean> => {
        let res: Response;
        try {
          res = await fetch(
            `${agentHttpBase()}/conversations/${cid}/report/audio/stream?mode=${mode}`,
            { method: "POST", headers, body: bodyPayload },
          );
        } catch {
          return false; // network / endpoint unavailable → fall back
        }
        if (!res.ok || !res.body) return false;

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let handled = false;
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let sep: number;
          while ((sep = buffer.indexOf("\n\n")) >= 0) {
            const frame = buffer.slice(0, sep);
            buffer = buffer.slice(sep + 2);
            const dataLine = frame
              .split("\n")
              .find((l) => l.startsWith("data:"));
            if (!dataLine) continue;
            let ev: {
              stage?: string;
              current?: number;
              total?: number;
              mp3_url?: string;
              reason?: string;
              // W-08: real voice-model download byte counters.
              downloaded?: number;
              pct?: number;
              file_index?: number;
              file_total?: number;
            };
            try {
              ev = JSON.parse(dataLine.slice(5).trim());
            } catch {
              continue;
            }
            if (ev.stage === "done" && ev.mp3_url) {
              setAudio({ status: "done", audioUrl: `${agentHttpBase()}${ev.mp3_url}` });
              handled = true;
            } else if (ev.stage === "error") {
              setAudio({
                status: "unavailable",
                reason: audioReasonMessage(ev.reason ?? "unknown"),
              });
              handled = true;
            } else if (ev.stage) {
              setAudio({
                status: "generating",
                progress: {
                  stage: ev.stage as AudioStage,
                  current: ev.current,
                  total: ev.stage === "downloading_model" ? undefined : ev.total,
                  downloaded: ev.downloaded,
                  bytesTotal: ev.stage === "downloading_model" ? ev.total : undefined,
                  pct: ev.pct,
                  fileIndex: ev.file_index,
                  fileTotal: ev.file_total,
                },
              });
            }
          }
        }
        return handled;
      };

      // Blocking fallback — the original single-shot POST.
      const viaBlocking = async (): Promise<void> => {
        const res = await fetch(
          `${agentHttpBase()}/conversations/${cid}/report/audio?mode=${mode}`,
          { method: "POST", headers, body: bodyPayload },
        );
        if (!res.ok) {
          let reason = `${res.status}`;
          try {
            const body = (await res.json()) as { detail?: { reason?: string } | string };
            const detail = body?.detail;
            if (typeof detail === "object" && detail !== null) {
              reason = detail.reason ?? reason;
            } else if (typeof detail === "string") {
              reason = detail;
            }
          } catch {
            /* opaque */
          }
          throw new Error(audioReasonMessage(reason));
        }
        const data = (await res.json()) as { mp3_url: string };
        setAudio({ status: "done", audioUrl: `${agentHttpBase()}${data.mp3_url}` });
      };

      try {
        const streamed = await viaStream();
        if (!streamed) await viaBlocking();
      } catch (e: unknown) {
        const reason = e instanceof Error ? e.message : String(e);
        setAudio({ status: "unavailable", reason });
      }
    },
    [cid, followUpSeqs, onModeOpenChange],
  );

  const reset = useCallback(() => setAudio({ status: "idle" }), []);

  // B3: AudioModeDialog is ALWAYS rendered (outside the status branches) so
  // it remains reachable after audio is generated, not just in the idle branch.
  // Radix Dialog handles open/close via the `open` prop; unmounting it would
  // lose any open-animation state and break the dialog after first generation.
  return (
    // Gap #46: data-tts-state reflects the TTS lifecycle (idle → generating →
    // done | unavailable). `display:contents` so the marker carries the attr
    // without perturbing the flex layout of the action row.
    <div className="contents" data-tts-state={audio.status}>
      <AudioModeDialog
        open={modeOpen}
        onOpenChange={onModeOpenChange}
        onChoose={generate}
      />

      {audio.status === "generating" && (() => {
        const pct = audioProgressPct(audio.progress);
        const isDownload = audio.progress?.stage === "downloading_model";
        return (
          <div
            className="flex min-w-[12rem] flex-col gap-hair"
            data-tts-stage={audio.progress?.stage ?? ""}
            data-tts-pct={pct != null ? Math.round(pct) : ""}
          >
            <div className="flex items-center gap-hair">
              <Loader2 className="size-3.5 animate-spin text-text-faint" aria-hidden />
              <span className="font-ui text-[0.78rem] text-text-muted">
                {audioStageLabel(audio.progress)}
              </span>
            </div>
            {/* W-08: REAL progress bar next to the button. Determinate fill for a
                voice-model download (actual bytes/percent) or synthesis (turn
                n/m); indeterminate (no bar) for the un-measurable prep/mix
                stages. Never fabricated — `pct` is the server's byte count. */}
            {pct != null && (
              <div
                className="h-1 w-full overflow-hidden rounded-full bg-hairline"
                role="progressbar"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Math.round(pct)}
                aria-label={isDownload ? "Voice model download progress" : "Audio synthesis progress"}
              >
                <div
                  className="h-full rounded-full bg-accent transition-[width] duration-200"
                  style={{ width: `${pct}%` }}
                />
              </div>
            )}
            {/* The ~300 MB note is HONEST — shown only during a genuine first-run
                voice-model download, with the real running byte count. */}
            {isDownload && (
              <span className="font-ui text-[0.73rem] tabular-nums text-text-faint">
                {audio.progress?.bytesTotal
                  ? `${fmtMB(audio.progress.downloaded)} / ${fmtMB(audio.progress.bytesTotal)}`
                  : "Downloading voice model (~300 MB, first run only)"}
                {" — this may take a minute."}
              </span>
            )}
          </div>
        );
      })()}

      {audio.status === "done" && (
        // Real play + export affordances — gated on a real audio URL, never a fake.
        <div className="flex flex-wrap items-center gap-inline">
          {/* WALK-14 / D2: custom AudioPlayer instead of native <audio controls> */}
          <AudioPlayer src={audio.audioUrl} className="max-w-[18rem] flex-1" />
          <button
            type="button"
            onClick={() => {
              // B4: filename derived from report title, not hardcoded "audio-overview.mp3".
              const a = document.createElement("a");
              a.href = audio.audioUrl;
              a.download = sanitizeFilename(reportQuery) + ".mp3";
              document.body.appendChild(a);
              a.click();
              a.remove();
            }}
            data-disco-control="dr.audio.export"
            className={CTRL_BTN}
          >
            <ArrowDown className="size-3.5" aria-hidden />
            Export MP3
          </button>
          <button
            type="button"
            onClick={reset}
            data-disco-control="dr.audio.regenerate"
            className={CTRL_BTN}
          >
            <Headphones className="size-3.5" aria-hidden />
            Regenerate
          </button>
        </div>
      )}

      {audio.status === "unavailable" && (
        // Honest error, NO false success, resettable
        <div className="flex flex-col gap-hair">
          <div className="flex flex-wrap items-center gap-inline">
            <Headphones className="size-3.5 shrink-0 text-text-faint" aria-hidden />
            <span className="font-ui text-[0.78rem] text-text-faint">{audio.reason}</span>
            <button
              type="button"
              onClick={reset}
              aria-label="Reset audio overview state"
              className={CTRL_BTN}
            >
              Try again
            </button>
          </div>
        </div>
      )}
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
      const newCid = await createBuildConversation(null, "agent", true);
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
