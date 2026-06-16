import { Fragment, type ReactNode } from "react";

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

/** A fenced code block with minimal inline syntax highlighting. */
export function CodeBlock({ language, code }: { language: string; code: string }) {
  return (
    <div className="overflow-hidden rounded-card border border-hairline bg-surface-1">
      <div className="flex items-center justify-between border-b border-hairline px-body py-hair">
        <span className="font-mono text-[0.7rem] uppercase tracking-wide text-text-faint">
          {language}
        </span>
      </div>
      <pre className="overflow-x-auto px-body py-inline font-mono text-[0.82rem] leading-relaxed text-text">
        <code>{highlight(code)}</code>
      </pre>
    </div>
  );
}
