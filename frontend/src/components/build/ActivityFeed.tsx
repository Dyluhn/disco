/**
 * The Agent Activity Feed (BoD §13.4): a unified chat-and-action log. User
 * messages (steer/send_message), agent prose (ask_user free-form replies), and
 * tool calls are interleaved chronologically. The agent's NATURAL-LANGUAGE
 * THOUGHT is always shown wrapped, NEVER truncated — that's the model talking,
 * not metadata. Tool calls + observations can be expanded inline so users can
 * see the raw command + result without losing the feed's narrative flow.
 *
 * Confidence gradient still drives attention: routine done steps recede,
 * pending/failed/risky steps stand out.
 */

import { useState } from "react";
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronRight,
  Circle,
  Download,
  File,
  FileSpreadsheet,
  Loader2,
  MessageSquare,
  MonitorPlay,
  MousePointerClick,
  Paperclip,
  User,
  X,
} from "lucide-react";
import { cn } from "@/lib/cn";
import { splitThink } from "@/lib/think";
import { agentHttpBase } from "@/api/client";
import { DeckExportBar } from "@/components/build/DeckExportBar";
import { rendererLabel } from "@/lib/slidesRenderer";
import type { ActivityItem } from "@/lib/buildTrace";

function ScreenshotThumbnail({
  path,
  conversationId,
}: {
  path: string;
  conversationId: string;
}) {
  const [failed, setFailed] = useState(false);
  const src = `${agentHttpBase()}/conversations/${conversationId}/workspace/${path}`;
  if (failed) {
    return (
      <span className="font-ui text-[0.74rem] text-text-faint italic">
        screenshot no longer available
      </span>
    );
  }
  return (
    <a href={src} target="_blank" rel="noreferrer" className="mt-hair block">
      <img
        src={src}
        alt="browser screenshot"
        onError={() => setFailed(true)}
        className="max-h-[200px] w-auto rounded border border-hairline object-contain"
      />
    </a>
  );
}

/** A generated spreadsheet — a real download via the declared-artifact route
 * (encodeURI preserves any subdir slashes). Honest: only renders when there's a
 * conversation id to fetch against; the file's live formulas compute on open.
 *
 * Exported (D12) so SheetBlock (AnswerDocument path) can reuse the EXACT same
 * download affordance when a cid is threaded down — no fork, no divergence. */
export function SheetDownload({
  sheet,
  conversationId,
}: {
  sheet: NonNullable<ActivityItem["expandable"]>["sheet"];
  conversationId: string;
}) {
  if (!sheet) return null;
  const href = `${agentHttpBase()}/conversations/${conversationId}/artifacts/${encodeURI(sheet.filename)}`;
  const n = sheet.sheet_names?.length ?? 0;
  return (
    <a
      href={href}
      download
      data-disco-control="build.activity-download"
      data-download-kind="sheet"
      className="mt-hair flex items-center gap-inline rounded-card border border-hairline bg-surface-0 px-inline py-hair transition-colors hover:border-hairline-strong"
    >
      <FileSpreadsheet className="size-4 shrink-0 text-accent" aria-hidden />
      <span className="min-w-0 flex-1">
        <span className="block truncate font-ui text-[0.82rem] text-text">
          {sheet.title || sheet.filename}
        </span>
        <span className="block truncate font-mono text-[0.7rem] text-text-faint">
          {sheet.filename}
          {n > 0 ? ` · ${n} sheet${n !== 1 ? "s" : ""}` : ""}
        </span>
      </span>
      <Download className="size-3.5 shrink-0 text-text-faint" aria-hidden />
    </a>
  );
}

/** A generated slide deck — a real download via the declared-artifact route
 * (encodeURI preserves any subdir slashes). Honest: only renders when there's a
 * conversation id to fetch against; the file is whatever the slides backend
 * emitted (HTML / PDF / PPTX), and the format is shown so the user knows what
 * they'll get.
 *
 * Exported (D2) so SlidesBlock (AnswerDocument path) can reuse the EXACT same
 * download affordance when a cid is threaded down — same pattern as
 * SheetDownload: not a fork, no divergence. The declared-artifact route is
 * `/conversations/{cid}/artifacts/{filename}` (app.py; allowlisted to emitted
 * .html/.pdf/.pptx artifacts) — the same route the .xlsx download uses. */
export function SlidesDownload({
  slides,
  conversationId,
}: {
  slides: NonNullable<ActivityItem["expandable"]>["slides"];
  conversationId: string;
}) {
  if (!slides) return null;
  const n = slides.slide_count ?? slides.slides?.length ?? 0;
  // An editable deck (authored sidecar) can be re-rendered with any template at
  // download time via /deck/export — pure render-on-demand, no live sandbox needed.
  // A non-editable deck (Marp) keeps the baked artifact link (no false affordance).
  const editable = Boolean(slides.editable && slides.base);
  // R7: be honest about a DEGRADED fallback deck (Marp CLI unavailable) vs a real
  // structured render — otherwise the user can't tell a real deck from "HTML fake slides".
  const rl = rendererLabel(slides.renderer);
  const meta =
    n > 0 ? `${slides.filename} · ${n} slide${n !== 1 ? "s" : ""}` : slides.filename;

  // W-16: an editable deck → the SHARED DeckExportBar (Theme picker + real pptx/html
  // render-on-demand download links). One component, also used by the in-app editor.
  if (editable) {
    return (
      <div className="mt-hair flex flex-col gap-hair">
        {!rl.real && (
          <p className="flex items-center gap-hair font-ui text-[0.7rem] text-unsupported">
            <AlertTriangle className="size-3 shrink-0" aria-hidden />
            Built with {rl.long} — not the full structured deck.
          </p>
        )}
        <DeckExportBar
          conversationId={conversationId}
          base={slides.base!}
          title={slides.title || slides.filename}
        />
        <span className="truncate px-inline font-mono text-[0.7rem] text-text-faint">{meta}</span>
      </div>
    );
  }

  // Non-editable deck (Marp/fallback) → the baked artifact link (no template re-render).
  const staticHref = `${agentHttpBase()}/conversations/${conversationId}/artifacts/${encodeURI(slides.filename)}`;
  const fmt = (slides.format || "html").toUpperCase();
  return (
    <div className="mt-hair flex flex-col gap-hair">
      <a
        href={staticHref}
        download
        data-disco-control="build.activity-download"
        data-download-kind="slides"
        className="flex items-center gap-inline rounded-card border border-hairline bg-surface-0 px-inline py-hair transition-colors hover:border-hairline-strong"
      >
        <MonitorPlay className="size-4 shrink-0 text-accent" aria-hidden />
        <span className="min-w-0 flex-1">
          <span className="block truncate font-ui text-[0.82rem] text-text">
            {slides.title || slides.filename}
          </span>
          <span className="block truncate font-mono text-[0.7rem] text-text-faint">
            {slides.filename}
            {n > 0 ? ` · ${fmt} · ${n} slide${n !== 1 ? "s" : ""}` : ` · ${fmt}`}
          </span>
        </span>
        <Download className="size-3.5 shrink-0 text-text-faint" aria-hidden />
      </a>
      {!rl.real && (
        <p className="flex items-center gap-hair font-ui text-[0.7rem] text-unsupported">
          <AlertTriangle className="size-3 shrink-0" aria-hidden />
          Built with {rl.long} — not the full structured deck.
        </p>
      )}
    </div>
  );
}

/** F2: An agent-emitted file (via serve(kind="files")) — a real download via the
 * declared-artifact route. Renders as a first-class download card in the conversation
 * feed. Only renders when there's a conversation id (no false affordance). */
export function FileDownload({
  file,
  conversationId,
}: {
  file: NonNullable<ActivityItem["expandable"]>["file"];
  conversationId: string;
}) {
  if (!file) return null;
  const href = `${agentHttpBase()}/conversations/${conversationId}/artifacts/${encodeURI(file.filename)}`;
  return (
    <a
      href={href}
      download
      data-disco-control="build.activity-download"
      data-download-kind="file"
      className="mt-hair flex items-center gap-inline rounded-card border border-hairline bg-surface-0 px-inline py-hair transition-colors hover:border-hairline-strong"
    >
      <File className="size-4 shrink-0 text-accent" aria-hidden />
      <span className="min-w-0 flex-1">
        <span className="block truncate font-ui text-[0.82rem] text-text">
          {file.title || file.filename}
        </span>
        <span className="block truncate font-mono text-[0.7rem] text-text-faint">
          {file.filename}
        </span>
      </span>
      <Download className="size-3.5 shrink-0 text-text-faint" aria-hidden />
    </a>
  );
}

function StatusDot({ item }: { item: ActivityItem }) {
  if (item.kind === "user")
    return <User className="size-3.5 text-accent" aria-label="you" />;
  if (item.kind === "agent_message")
    return <MessageSquare className="size-3.5 text-text-muted" aria-label="agent message" />;
  if (item.kind === "system_warning")
    return <AlertTriangle className="size-3.5 text-warn" aria-label="warning" />;
  if (item.kind === "system_note")
    // Neutral confirmation (e.g. BP-11 upload announcement) — informational
    // paperclip, NOT the warning triangle: this is not an alarm.
    return <Paperclip className="size-3.5 text-text-muted" aria-label="note" />;
  if (item.status === "running")
    return <Loader2 className="size-3.5 animate-spin text-text-muted" aria-label="running" />;
  if (item.status === "pending_send")
    return <Loader2 className="size-3.5 animate-spin text-text-faint" aria-label="sending" />;
  if (item.status === "pending")
    return <AlertTriangle className="size-3.5 text-warn" aria-label="awaiting approval" />;
  if (item.status === "failed") return <X className="size-3.5 text-unsupported" aria-label="failed" />;
  return <Check className="size-3.5 text-supported" aria-label="done" />;
}

function ExpandableDetail({ item }: { item: ActivityItem }) {
  const [open, setOpen] = useState(false);
  const e = item.expandable;
  if (!e) return null;
  // Auto-open failed actions so the user sees what broke without clicking.
  const isOpen = open || item.status === "failed";
  const hasOutput = !!(e.output || e.error);
  // Format arguments inline for the summary; pretty-printed JSON in the body.
  const argsPreview = (() => {
    if (e.tool_name === "shell") return String(e.arguments.command ?? "");
    if (e.tool_name === "search") return `"${String(e.arguments.query ?? "")}"`;
    if (e.tool_name === "extract") return String(e.arguments.url ?? "");
    if (e.tool_name.startsWith("file_")) return String(e.arguments.path ?? "");
    const argText = JSON.stringify(e.arguments);
    // "{}" as the toggle row is raw noise (UI sweep: "Listed workflows / {}") —
    // an argless call still gets its expander, labeled in words.
    if (argText === "{}") return "details";
    return argText.length > 80 ? argText.slice(0, 80) + "…" : argText;
  })();

  return (
    <div className="mt-hair">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-hair font-mono text-[0.74rem] text-text-faint transition-colors hover:text-text-muted"
      >
        {isOpen ? (
          <ChevronDown className="size-3 shrink-0" aria-hidden />
        ) : (
          <ChevronRight className="size-3 shrink-0" aria-hidden />
        )}
        <span className="truncate text-left">{argsPreview}</span>
      </button>
      {isOpen && (
        <div className="mt-hair flex flex-col gap-hair rounded-control border border-hairline bg-surface-2 p-inline">
          <div>
            <span className="font-ui text-[0.7rem] uppercase tracking-wide text-text-faint">
              {e.tool_name}
            </span>
            <pre className="mt-px overflow-x-auto whitespace-pre-wrap break-words font-mono text-[0.74rem] leading-snug text-text-muted">
              {JSON.stringify(e.arguments, null, 2)}
            </pre>
          </div>
          {hasOutput && (
            <div>
              <span
                className={cn(
                  "font-ui text-[0.7rem] uppercase tracking-wide",
                  e.error ? "text-unsupported" : "text-text-faint",
                )}
              >
                {e.error ? "Error" : "Output"}
              </span>
              {item.status === "failed" && e.plainError && (
                <p className="mt-px whitespace-pre-wrap break-words font-ui text-[0.8rem] leading-snug text-text">
                  {e.plainError}
                </p>
              )}
              <pre
                className={cn(
                  "mt-px max-h-64 overflow-auto whitespace-pre-wrap break-words font-mono text-[0.74rem] leading-snug",
                  e.error ? "text-unsupported" : "text-text-muted",
                )}
              >
                {e.error || e.output}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** W-02: render an agent thought, splitting any inline `<think>` reasoning into
 * a collapsed (default-closed) "Thinking" details so raw tags never leak. */
function Thought({ text }: { text: string }) {
  const { reasoning, answer } = splitThink(text);
  return (
    <>
      {answer && (
        <p className="whitespace-pre-wrap break-words font-ui text-[0.84rem] leading-snug text-text">
          {answer}
        </p>
      )}
      {reasoning && (
        <details className="group mt-hair">
          <summary className="flex cursor-pointer list-none items-center gap-hair font-ui text-[0.72rem] uppercase tracking-wide text-text-faint hover:text-text-muted">
            <ChevronRight className="size-3 transition-transform group-open:rotate-90" aria-hidden />
            Thinking
          </summary>
          <p className="mt-hair whitespace-pre-wrap break-words border-l-2 border-hairline pl-body font-ui text-[0.8rem] leading-snug text-text-muted">
            {reasoning}
          </p>
        </details>
      )}
    </>
  );
}

export function ActivityFeed({
  items,
  conversationId,
}: {
  items: ActivityItem[];
  conversationId?: string;
}) {
  if (items.length === 0) {
    return (
      <p className="flex items-center gap-hair px-px font-ui text-[0.82rem] text-text-faint">
        <Circle className="size-3 animate-pulse" aria-hidden />
        Planning the first step…
      </p>
    );
  }
  return (
    <ol className="flex flex-col">
      {items.map((item, i) => {
        if (item.kind === "rollback_marker") {
          return (
            <li key={item.id} className="pmx-rise flex justify-center py-hair">
              <span className="rounded-full border border-warn/30 bg-warn/10 px-inline py-px font-ui text-[0.72rem] text-warn">
                {item.label}
              </span>
            </li>
          );
        }
        const isMessage =
          item.kind === "user" ||
          item.kind === "agent_message" ||
          item.kind === "system_warning" ||
          item.kind === "system_note";
        return (
          <li
            key={item.id}
            className={cn(
              "pmx-rise flex items-start gap-inline border-l-2 py-inline pl-body",
              item.attention
                ? "border-warn/60"
                : item.kind === "user"
                  ? "border-accent/40"
                  : "border-transparent",
              item.status === "pending_send" && "opacity-70",
            )}
          >
            <span className="mt-px shrink-0">
              <StatusDot item={item} />
            </span>
            <div className="flex min-w-0 flex-1 flex-col gap-hair">
              {item.kind === "user" && (
                <span className="font-ui text-[0.7rem] uppercase tracking-wide text-accent">
                  You
                  {item.status === "pending_send" && (
                    <span className="ml-hair font-ui text-text-faint normal-case tracking-normal">
                      sending…
                    </span>
                  )}
                </span>
              )}
              {item.kind === "agent_message" && (
                <span className="font-ui text-[0.7rem] uppercase tracking-wide text-text-muted">
                  Agent
                </span>
              )}
              {item.kind === "system_warning" && (
                <span className="font-ui text-[0.7rem] uppercase tracking-wide text-warn">
                  Warning
                </span>
              )}
              {item.kind === "system_note" && (
                <span className="font-ui text-[0.7rem] uppercase tracking-wide text-text-muted">
                  Note
                </span>
              )}
              {item.mention && (
                <span className="inline-flex max-w-full items-center gap-hair self-start rounded-full border border-hairline bg-surface-2 px-inline py-px font-ui text-[0.72rem] text-text-muted">
                  <MousePointerClick className="size-3 shrink-0" aria-hidden />
                  <span className="shrink-0 font-mono">{`<${item.mention.tag}>`}</span>
                  {item.mention.text && (
                    <>
                      <span className="shrink-0">·</span>
                      <span className="max-w-[40ch] truncate">{`"${item.mention.text}"`}</span>
                    </>
                  )}
                </span>
              )}
              <span
                className={cn(
                  "font-ui text-[0.88rem] leading-snug",
                  item.attention ? "font-medium text-text" : "text-text-muted",
                  item.status === "failed" && "text-unsupported",
                  isMessage && "whitespace-pre-wrap text-text",
                )}
              >
                {item.label}
                {item.status === "pending" && (
                  <span className="ml-inline font-ui text-[0.7rem] uppercase tracking-wide text-warn">
                    needs approval
                  </span>
                )}
                {item.autoApproved && (
                  <span className="ml-inline font-ui text-[0.7rem] uppercase tracking-wide text-text-faint">
                    auto · sandboxed
                  </span>
                )}
              </span>
              {/* The agent's natural-language thought — NEVER truncated, always
                  shown wrapped. Swallowing this was the most painful UX bug.
                  W-02: models that inline raw <think> tags (Qwen-style) get split
                  here — the answer renders normally, the reasoning collapses into
                  a closed "Thinking" details (kept, not deleted). */}
              {item.thought && !isMessage && <Thought text={item.thought} />}
              {/* The technical detail row — a short single-line label like a
                  file path or command preview. */}
              {item.detail && !isMessage && !item.thought && (
                <span className="truncate font-ui text-[0.78rem] leading-snug text-text-faint">
                  {item.detail}
                </span>
              )}
              {/* BP-15: screenshot thumbnail — always visible (not gated by expand).
                  Rendered below the label/thought so it reads inline in the feed. */}
              {item.expandable?.screenshot_path && conversationId && (
                <ScreenshotThumbnail
                  path={item.expandable.screenshot_path}
                  conversationId={conversationId}
                />
              )}
              {/* rp-11: a generated spreadsheet → an honest download card (only when
                  there's a cid to fetch the declared artifact against). */}
              {item.expandable?.sheet && conversationId && (
                <SheetDownload sheet={item.expandable.sheet} conversationId={conversationId} />
              )}
              {/* D2: a generated slide deck → an honest download card (same pattern
                  as SheetDownload, same declared-artifact route). Only when there's
                  a cid to fetch the declared artifact against — otherwise the
                  button stays absent (no false affordance). */}
              {item.expandable?.slides && conversationId && (
                <SlidesDownload slides={item.expandable.slides} conversationId={conversationId} />
              )}
              {/* F2: an agent-emitted file (via serve(kind="files")) → an honest
                  download card in the conversation feed. */}
              {item.expandable?.file && conversationId && (
                <FileDownload file={item.expandable.file} conversationId={conversationId} />
              )}
              {/* Expandable raw command + output drill-down. */}
              {item.expandable && <ExpandableDetail item={item} />}
            </div>
            {i === items.length - 1 && item.status === "running" && (
              <span className="ml-auto shrink-0 font-ui text-[0.7rem] uppercase tracking-wide text-text-faint">
                now
              </span>
            )}
          </li>
        );
      })}
    </ol>
  );
}
