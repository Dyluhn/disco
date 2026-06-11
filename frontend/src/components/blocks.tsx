import { Fragment, type ReactNode, useEffect, useRef, useState, useMemo } from "react";
import { Chart, registerables } from "chart.js";
import { cn } from "@/lib/cn";
import { citationNumbers, passageById, verdictForPassage } from "@/lib/sources";
import type { AnswerBlock, GroundedAnswer } from "@/types/grounded";
import { Citation } from "./Citation";

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

function ChartBlockComponent({
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

export function BlockView({ block, answer }: { block: AnswerBlock; answer: GroundedAnswer | null }) {
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
  }
}
