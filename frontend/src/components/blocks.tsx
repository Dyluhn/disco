import { Fragment, type ReactNode, useEffect, useRef, useState, useMemo } from "react";
import { Chart, registerables } from "chart.js";
import { cn } from "@/lib/cn";
import { citationNumbers, passageById, verdictForPassage } from "@/lib/sources";
import type { AnswerBlock, GroundedAnswer } from "@/types/grounded";
import { Citation } from "./Citation";
import { SheetDownload, SlidesDownload } from "./build/ActivityFeed";
import { FileSpreadsheet, AlertTriangle, ChevronLeft, ChevronRight, MonitorPlay } from "lucide-react";

Chart.register(...registerables);

const CITE = /\[\[(\w+)\]\]/g;
// Inline markdown the model actually emits: **bold**, `code`, *italic*. Bold is
// matched before italic so `**x**` isn't read as two italics. Citations are split
// out before this runs, so a span never straddles a chip.
const INLINE_MD = /\*\*([^*]+)\*\*|`([^`]+)`|\*([^*\n]+)\*/g;

/** Render a plain text run with inline markdown turned into real elements. */
function inlineMd(text: string, keyBase: number): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let key = keyBase;
  let m: RegExpExecArray | null;
  INLINE_MD.lastIndex = 0;
  while ((m = INLINE_MD.exec(text))) {
    if (m.index > last) out.push(<Fragment key={key++}>{text.slice(last, m.index)}</Fragment>);
    const [full, bold, code, italic] = m;
    if (bold !== undefined) {
      out.push(
        <strong key={key++} className="font-semibold text-text">
          {bold}
        </strong>,
      );
    } else if (code !== undefined) {
      out.push(
        <code key={key++} className="rounded bg-surface-1 px-1 font-mono text-[0.85em] text-text">
          {code}
        </code>,
      );
    } else {
      out.push(<em key={key++}>{italic}</em>);
    }
    last = m.index + full.length;
  }
  if (last < text.length) out.push(<Fragment key={key++}>{text.slice(last)}</Fragment>);
  return out;
}

/** Individual citation marker (the [n] chip or a placeholder). */
export function CitationMarker({ id, answer }: { id: string; answer: GroundedAnswer | null }) {
  const numbers = answer ? citationNumbers(answer) : null;
  const passage = answer ? passageById(answer, id) : undefined;
  if (answer && passage && numbers) {
    return (
      <Citation
        n={numbers.get(id) ?? 0}
        passage={passage}
        verdict={verdictForPassage(answer, id)}
      />
    );
  }
  return (
    <sup>
      <span className="mx-px inline-flex min-w-4 justify-center rounded-[0.25rem] border border-hairline px-1 align-super font-ui text-[0.62rem] leading-none text-text-faint">
        ·
      </span>
    </sup>
  );
}

/** Render prose text, replacing inline `[[id]]` markers with anchored citations.
 * Before the final answer arrives, render same-sized neutral placeholders so the
 * chips don't shift when provenance resolves (zero CLS). */
export function CitedText({ text, answer }: { text: string; answer: GroundedAnswer | null }) {
  const out: ReactNode[] = [];
  let last = 0;
  let key = 0;
  let m: RegExpExecArray | null;
  CITE.lastIndex = 0;
  while ((m = CITE.exec(text))) {
    out.push(<Fragment key={key++}>{inlineMd(text.slice(last, m.index), key * 1000)}</Fragment>);
    const id = m[1];
    out.push(<CitationMarker key={key++} id={id} answer={answer} />);
    last = m.index + m[0].length;
  }
  out.push(<Fragment key={key++}>{inlineMd(text.slice(last), key * 1000)}</Fragment>);
  return <>{out}</>;
}

// Combined token regex: strings | comments | keywords | numbers.
const TOKEN =
  /("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')|(#.*|\/\/.*)|(\b(?:def|return|for|in|if|else|import|from|class|const|let|function|sorted|enumerate)\b)|(\b\d+\b)/g;

/** Minimal, dependency-free token highlight — keywords / strings / comments /
 * numbers. (A full tokenizer like shiki is a [VERIFY] follow-up.) */
function highlightLine(line: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let last = 0;
  let key = 0;
  let m: RegExpExecArray | null;
  TOKEN.lastIndex = 0;
  while ((m = TOKEN.exec(line))) {
    if (m.index > last) nodes.push(<Fragment key={key++}>{line.slice(last, m.index)}</Fragment>);
    const [full, str, comment, kw, num] = m;
    if (str) nodes.push(<span key={key++} className="text-supported">{str}</span>);
    else if (comment) nodes.push(<span key={key++} className="italic text-text-faint">{comment}</span>);
    else if (kw) nodes.push(<span key={key++} className="text-accent">{kw}</span>);
    else if (num) nodes.push(<span key={key++} className="text-weak">{num}</span>);
    last = m.index + full.length;
  }
  if (last < line.length) nodes.push(<Fragment key={key++}>{line.slice(last)}</Fragment>);
  return nodes;
}

function highlight(code: string): ReactNode[] {
  return code.split("\n").map((line, i) => {
    const nodes = highlightLine(line);
    return (
      <div key={i} className="min-h-[1.5em]">
        {nodes.length ? nodes : " "}
      </div>
    );
  });
}

function TableView({
  columns,
  rows,
  caption,
}: {
  columns: string[];
  rows: string[][];
  caption?: string;
}) {
  return (
    <figure className="m-0">
      <div className="overflow-x-auto rounded-card border border-hairline">
        <table className="w-full table-fixed border-collapse font-ui text-[0.85rem]">
          <thead>
            <tr className="bg-surface-1">
              {columns.map((c, i) => (
                <th
                  key={i}
                  className="border-b border-hairline px-body py-inline text-left font-semibold text-text-muted"
                >
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, ri) => (
              <tr key={ri} className="border-b border-hairline last:border-0">
                {row.map((cell, ci) => (
                  <td
                    key={ci}
                    className={cn(
                      "px-body py-inline align-top",
                      ci === 0 ? "font-medium text-text-muted" : "text-text",
                    )}
                  >
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {caption && (
        <figcaption className="mt-hair font-ui text-[0.74rem] text-text-faint">
          {caption}
        </figcaption>
      )}
    </figure>
  );
}

const COLORS = ["#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899"];

// Exported so Markdown.tsx can lift ```chart fences (deep-research report sections
// arrive as raw markdown, not structured blocks) into real inline charts.
export function ChartBlockComponent({
  chart_type,
  data,
  title,
  x_label,
  y_label,
}: {
  chart_type: string;
  data: any;
  title?: string;
  x_label?: string;
  y_label?: string;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [error, setError] = useState(false);

  const tableData = useMemo(() => {
    if (!data || !Array.isArray(data)) return null;
    try {
      if (chart_type === "scatter") {
        return {
          columns: ["Group", x_label || "X", y_label || "Y"],
          rows: data.map((d: any) => [String(d.group || "Default"), String(d.x), String(d.y)]),
        };
      }
      return {
        columns: [x_label || "Label", y_label || "Value"],
        rows: data.map((d: any) => [
          String(d.label || d.x || ""),
          String(d.value !== undefined ? d.value : d.y || ""),
        ]),
      };
    } catch {
      return null;
    }
  }, [data, chart_type, x_label, y_label]);

  useEffect(() => {
    if (error || !canvasRef.current || !data || !Array.isArray(data)) return;

    let chart: Chart | null = null;
    try {
      const ctx = canvasRef.current.getContext("2d");
      if (!ctx) return;

      const isScatter = chart_type === "scatter";
      const isPie = chart_type === "pie";

      chart = new Chart(ctx, {
        type: (chart_type as any) || "bar",
        data: {
          labels: isScatter ? [] : data.map((d: any) => d.label || d.x || ""),
          datasets: isScatter
            ? Array.from(new Set(data.map((d: any) => d.group || "Default"))).map((g, i) => ({
                label: String(g),
                data: data
                  .filter((d: any) => (d.group || "Default") === g)
                  .map((d: any) => ({ x: d.x, y: d.y })),
                backgroundColor: COLORS[i % COLORS.length],
              }))
            : [
                {
                  label: y_label || "Value",
                  data: data.map((d: any) => (d.value !== undefined ? d.value : d.y || 0)),
                  backgroundColor: isPie ? COLORS : COLORS[0],
                  borderColor: COLORS[0],
                  borderWidth: isPie ? 0 : 2,
                  tension: 0.1,
                },
              ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          plugins: {
            title: { display: !!title, text: title, color: "#e6edf3" },
            legend: {
              display: isPie || isScatter,
              labels: { color: "#8b949e", font: { family: "system-ui" } },
            },
            tooltip: {
              backgroundColor: "#161b22",
              titleColor: "#e6edf3",
              bodyColor: "#8b949e",
              borderColor: "#30363d",
              borderWidth: 1,
            },
          },
          scales: isPie
            ? {}
            : {
                x: {
                  title: { display: !!x_label, text: x_label, color: "#8b949e" },
                  ticks: { color: "#8b949e" },
                  grid: { color: "rgba(48, 54, 61, 0.5)" },
                },
                y: {
                  title: { display: !!y_label, text: y_label, color: "#8b949e" },
                  ticks: { color: "#8b949e" },
                  grid: { color: "rgba(48, 54, 61, 0.5)" },
                  beginAtZero: true,
                },
              },
        },
      });
    } catch (e) {
      console.error("Chart.js init failed", e);
      setError(true);
    }
    return () => chart?.destroy();
  }, [chart_type, data, title, x_label, y_label, error]);

  if (error || !tableData) {
    return tableData ? (
      <TableView columns={tableData.columns} rows={tableData.rows} caption={title} />
    ) : (
      <div className="rounded-card border border-hairline bg-surface-1 p-body text-text-faint italic">
        (Invalid chart data)
      </div>
    );
  }

  return (
    <div className="my-inline rounded-card border border-hairline bg-surface-1 p-body">
      <div className="h-[300px] w-full">
        <canvas ref={canvasRef} />
      </div>
      {title && (
        <div className="mt-hair text-center font-ui text-[0.74rem] text-text-faint">{title}</div>
      )}
    </div>
  );
}

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
function SheetBlockComponent({
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

/**
 * SlidesBlockComponent — renders a navigable slide-deck PREVIEW card. Each
 * slide the model emitted is shown in turn via prev/next controls; the actual
 * HTML / PDF / PPTX file is delivered as a workspace artifact
 * (ToolOutcome.artifacts → DeliverableEvent) and is downloaded through the
 * app's deliverable/workspace machinery, which holds the conversation id.
 *
 * D2: when a `cid` is threaded in, the in-block download reuses the
 * ActivityFeed `SlidesDownload` — the SAME component, not a fork. When no cid
 * is present (the standard research path), the button stays ABSENT — never a
 * false affordance (a self-fetching button with no cid would always 404). The
 * honest download lives where slides are actually made: the Build/Agent
 * ActivityFeed via the declared-artifact route (app.py
 * `/conversations/{cid}/artifacts/{path}`, allowlisted to emitted .html /
 * .pdf / .pptx artifacts) — see SlidesDownload in build/ActivityFeed.tsx
 * (rp-11 residue extended for slide decks).
 *
 * Inline embedding of the rendered HTML deck (an in-card iframe to the
 * artifact route) is still deferred: a PDF/PPTX in the same slot would 404
 * (the iframe can't render non-HTML), and a service-side slide rasteriser
 * (chromium→png/canvas) is out of scope. The card stays an honest preview
 * (prev/next through the inline `slides` list) + the (cid-gated) download.
 */
function SlidesBlockComponent({
  title,
  filename,
  format,
  slide_count,
  slides,
  renderer,
  cid,
}: {
  title: string;
  filename: string;
  format: "html" | "pdf" | "pptx";
  slide_count: number;
  /** Inline per-slide preview, threaded for in-block navigation. Empty
   *  array → the viewer degrades to a "slide N of M" counter (still
   *  navigable, just no content body). */
  slides: { title?: string; content?: string }[];
  renderer: "marp" | "fallback";
  /** The conversation id (cid) — when present, the in-block download
   *  resolves against the declared-artifact route. ABSENT = no download,
   *  no false affordance. */
  cid?: string | null;
}) {
  // Total slide count prefers the explicit `slide_count` (authoritative —
  // comes from the slides backend's split), then falls back to the inline
  // `slides` array length. Always ≥ 1 so the counter never reads "0 / 0".
  const total = Math.max(slide_count || 0, slides.length, 1);
  const [index, setIndex] = useState(0);
  const safeIndex = Math.min(Math.max(index, 0), total - 1);
  const current = slides[safeIndex];
  // Build the same shape ActivityFeed passes to SlidesDownload; this is the
  // single source of truth for the download link/affordance.
  const slidesMeta = {
    filename,
    title,
    format,
    slide_count: total,
    slides,
  };
  const goPrev = () => setIndex((i) => (i - 1 + total) % total);
  const goNext = () => setIndex((i) => (i + 1) % total);
  return (
    <div className="my-inline rounded-card border border-hairline bg-surface-1">
      {/* Header — the in-block download appears here ONLY when a cid is
          present. Reuses SlidesDownload verbatim (not a fork): same
          <a download>, same declared-artifact URL, same encodeURI
          semantics. */}
      <div className="flex items-center gap-inline border-b border-hairline px-body py-inline">
        <MonitorPlay className="size-5 shrink-0 text-accent" aria-hidden />
        <div className="min-w-0 flex-1">
          <div className="truncate font-ui text-[0.9rem] font-semibold text-text">{title}</div>
          <div className="truncate font-mono text-[0.72rem] text-text-faint">
            {filename} · {format.toUpperCase()} · {total} slide{total !== 1 ? "s" : ""}
          </div>
        </div>
        {cid && <SlidesDownload slides={slidesMeta} conversationId={cid} />}
      </div>

      {/* Slide nav: prev / counter / next. The counter is the source of
          truth; the buttons are the affordance. Wraps to total slides so
          the user can step past either end. */}
      <div className="flex items-center justify-between gap-inline border-b border-hairline px-body py-inline">
        <button
          type="button"
          onClick={goPrev}
          disabled={total <= 1}
          aria-label="Previous slide"
          className="flex items-center gap-hair rounded-control border border-hairline bg-surface-0 px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:border-hairline-strong disabled:cursor-not-allowed disabled:opacity-40"
        >
          <ChevronLeft className="size-3.5" aria-hidden />
          Prev
        </button>
        <div
          className="font-mono text-[0.78rem] text-text-faint"
          aria-live="polite"
          aria-label={`Slide ${safeIndex + 1} of ${total}`}
        >
          <span className="text-text-muted">{safeIndex + 1}</span>
          <span className="mx-1 text-text-faint">/</span>
          <span>{total}</span>
        </div>
        <button
          type="button"
          onClick={goNext}
          disabled={total <= 1}
          aria-label="Next slide"
          className="flex items-center gap-hair rounded-control border border-hairline bg-surface-0 px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:border-hairline-strong disabled:cursor-not-allowed disabled:opacity-40"
        >
          Next
          <ChevronRight className="size-3.5" aria-hidden />
        </button>
      </div>

      {/* Active slide preview. Renders the inline slide content when the
          model emitted it; otherwise the slide body is the format / counter
          (still honest — the user knows the deck has N slides and can step
          through). */}
      <div className="px-body py-inline">
        {current?.title && (
          <div className="mb-hair font-ui text-[0.72rem] font-semibold uppercase tracking-wide text-text-faint">
            {current.title}
          </div>
        )}
        {current?.content ? (
          <p className="prose-reading whitespace-pre-wrap text-[0.95rem]">
            {current.content}
          </p>
        ) : (
          <p className="font-ui text-[0.82rem] italic text-text-faint">
            Slide {safeIndex + 1} of {total} ({(format || "html").toUpperCase()} ·{" "}
            {renderer === "fallback" ? "fallback renderer" : "Marp-rendered"}).{" "}
            {cid
              ? "Download above to view the rendered slide."
              : "Open the deck in the build surface to view the rendered slide."}
          </p>
        )}
      </div>

      {/* Provenance + renderer hint */}
      <div className="flex items-start gap-hair border-t border-hairline px-body py-inline">
        <MonitorPlay className="mt-px size-3.5 shrink-0 text-text-faint" aria-hidden />
        <p className="font-ui text-[0.74rem] leading-snug text-text-faint">
          Saved to the workspace as{" "}
          <span className="font-mono text-text-muted">{filename}</span>. Rendered by{" "}
          {renderer === "fallback" ? "the HTML fallback renderer" : "Marp"}.
        </p>
      </div>
    </div>
  );
}

export function BlockView({
  block,
  answer,
  cid,
}: {
  block: AnswerBlock;
  answer: GroundedAnswer | null;
  /** Conversation id, threaded down to SheetBlock for the in-block download.
   *  ABSENT = no download (no false affordance). */
  cid?: string | null;
}) {
  switch (block.kind) {
    case "heading":
      // Item D — section headers in the DISPLAY SERIF so the whole reading column
      // (serif title → serif headers → serif body) is typographically coherent;
      // the grotesk stays reserved for UI/chrome/labels.
      return (
        <h2
          id={block.id}
          className={cn(
            "scroll-mt-24 font-display font-medium text-text ui-tight",
            block.level === 2 ? "text-[1.5rem]" : "text-[1.2rem]",
          )}
        >
          {block.text}
        </h2>
      );
    case "prose":
      return (
        <p className="prose-reading">
          <CitedText text={block.text} answer={answer} />
        </p>
      );
    case "code":
      return (
        <div className="overflow-hidden rounded-card border border-hairline bg-surface-1">
          <div className="flex items-center justify-between border-b border-hairline px-body py-hair">
            <span className="font-mono text-[0.7rem] uppercase tracking-wide text-text-faint">
              {block.language}
            </span>
          </div>
          <pre className="overflow-x-auto px-body py-inline font-mono text-[0.82rem] leading-relaxed text-text">
            <code>{highlight(block.code)}</code>
          </pre>
        </div>
      );
    case "callout":
      return (
        <aside
          className={cn(
            "rounded-card border border-hairline bg-surface-1 p-body",
            block.tone === "warning" ? "border-l-2 border-l-warn" : "border-l-2 border-l-accent/50",
          )}
        >
          <div className="mb-hair font-ui text-[0.72rem] font-semibold uppercase tracking-wide text-text-faint">
            {block.title}
          </div>
          <p className="prose-reading text-[0.95rem]">{block.text}</p>
        </aside>
      );
    case "chart":
      return (
        <ChartBlockComponent
          chart_type={block.chart_type}
          data={block.data}
          title={block.title}
          x_label={block.x_label}
          y_label={block.y_label}
        />
      );
    case "table":
      return <TableView columns={block.columns} rows={block.rows} caption={block.caption} />;
    case "sheet":
      return (
        <SheetBlockComponent
          title={block.title}
          filename={block.filename}
          sheet_names={block.sheet_names}
          cid={cid}
        />
      );
    case "slides":
      return (
        <SlidesBlockComponent
          title={block.title}
          filename={block.filename}
          format={block.format}
          slide_count={block.slide_count}
          slides={block.slides}
          renderer={block.renderer}
          cid={cid}
        />
      );
  }
}
