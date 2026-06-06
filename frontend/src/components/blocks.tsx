import { Fragment, type ReactNode } from "react";
import { cn } from "@/lib/cn";
import { citationNumbers, passageById, verdictForPassage } from "@/lib/sources";
import type { AnswerBlock, GroundedAnswer } from "@/types/grounded";
import { Citation } from "./Citation";

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

/** Render prose text, replacing inline `[[id]]` markers with anchored citations.
 * Before the final answer arrives, render same-sized neutral placeholders so the
 * chips don't shift when provenance resolves (zero CLS). */
export function CitedText({ text, answer }: { text: string; answer: GroundedAnswer | null }) {
  const numbers = answer ? citationNumbers(answer) : null;
  const out: ReactNode[] = [];
  let last = 0;
  let key = 0;
  let m: RegExpExecArray | null;
  CITE.lastIndex = 0;
  while ((m = CITE.exec(text))) {
    out.push(<Fragment key={key++}>{inlineMd(text.slice(last, m.index), key * 1000)}</Fragment>);
    const id = m[1];
    const passage = answer ? passageById(answer, id) : undefined;
    if (answer && passage && numbers) {
      out.push(
        <Citation
          key={key++}
          n={numbers.get(id) ?? 0}
          passage={passage}
          verdict={verdictForPassage(answer, id)}
        />,
      );
    } else {
      out.push(
        <sup key={key++}>
          <span className="mx-px inline-flex min-w-4 justify-center rounded-[0.25rem] border border-hairline px-1 align-super font-ui text-[0.62rem] leading-none text-text-faint">
            ·
          </span>
        </sup>,
      );
    }
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
    case "table":
      return (
        <figure className="m-0">
          <div className="overflow-x-auto rounded-card border border-hairline">
            <table className="w-full table-fixed border-collapse font-ui text-[0.85rem]">
              <thead>
                <tr className="bg-surface-1">
                  {block.columns.map((c, i) => (
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
                {block.rows.map((row, ri) => (
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
          {block.caption && (
            <figcaption className="mt-hair font-ui text-[0.74rem] text-text-faint">
              {block.caption}
            </figcaption>
          )}
        </figure>
      );
  }
}
