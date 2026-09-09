/**
 * Markdown → token-styled elements. The agent's messages are free-form markdown (bold,
 * inline + fenced code, lists, links, tables); rendering them literally was the bug. This
 * maps every element to the design tokens so it reads as part of the product, dark + light.
 */

import React, { useMemo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/cn";
import { ChartBlockComponent } from "./blocks/ChartBlock";
import { CitationMarker } from "./blocks/CitedText";
import type { ChartDatum, GroundedAnswer } from "@/types/grounded";

/** Parse a ```chart fence's JSON payload, or null when it isn't a valid chart.
 * Deep-research report sections arrive as RAW markdown (the synthesis prompt embeds
 * charts as ```chart fenced JSON), so the lift to a real chart must happen here —
 * unlike the standard research surface, which gets structured chart blocks. */
function parseChartPayload(node: React.ReactNode): {
  chart_type: string;
  data: ChartDatum[];
  title?: string;
  x_label?: string;
  y_label?: string;
} | null {
  if (!React.isValidElement(node)) return null;
  const props = node.props as { className?: string; children?: React.ReactNode };
  if (!props.className?.includes("language-chart")) return null;
  const raw = typeof props.children === "string" ? props.children : null;
  if (!raw) return null;
  try {
    const payload = JSON.parse(raw);
    if (payload && typeof payload === "object" && payload.chart_type && payload.data) {
      return payload;
    }
  } catch {
    /* malformed → fall through to an honest code fence */
  }
  return null;
}

const CITE = /\[\[(\w+)\]\]/g;

/** Recursively traverse React children to find string nodes and replace [[id]] with CitationMarker. */
function processCitations(children: React.ReactNode, answer: GroundedAnswer | null): React.ReactNode {
  return React.Children.map(children, (child) => {
    if (typeof child === "string") {
      const parts = child.split(CITE);
      if (parts.length === 1) return child;
      const result: React.ReactNode[] = [];
      for (let i = 0; i < parts.length; i++) {
        if (i % 2 === 0) {
          if (parts[i]) result.push(parts[i]);
        } else {
          result.push(<CitationMarker key={i} id={parts[i]} answer={answer} />);
        }
      }
      return result;
    }
    if (React.isValidElement<{ children?: React.ReactNode }>(child) && child.props.children) {
      // Skip recursion for code/pre blocks
      if (child.type === "code" || child.type === "pre") return child;
      return React.cloneElement(child, {
        children: processCitations(child.props.children, answer),
      });
    }
    return child;
  });
}

const COMPONENTS: Components = {
  p: ({ children }) => <p className="my-inline leading-relaxed first:mt-0 last:mb-0">{children}</p>,
  strong: ({ children }) => <strong className="font-semibold text-text">{children}</strong>,
  em: ({ children }) => <em className="italic">{children}</em>,
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noreferrer" className="text-accent underline underline-offset-2 hover:opacity-80">
      {children}
    </a>
  ),
  ul: ({ children }) => <ul className="my-inline list-disc space-y-px pl-5">{children}</ul>,
  ol: ({ children }) => <ol className="my-inline list-decimal space-y-px pl-5">{children}</ol>,
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  h1: ({ children }) => <h3 className="mb-inline mt-body font-display text-[1.1rem] font-medium text-text first:mt-0">{children}</h3>,
  h2: ({ children }) => <h3 className="mb-inline mt-body font-display text-[1.05rem] font-medium text-text first:mt-0">{children}</h3>,
  h3: ({ children }) => <h4 className="mb-hair mt-body font-ui text-[0.95rem] font-semibold text-text first:mt-0">{children}</h4>,
  blockquote: ({ children }) => (
    <blockquote className="my-inline border-l-2 border-hairline-strong pl-body text-text-muted">{children}</blockquote>
  ),
  hr: () => <hr className="my-body border-hairline" />,
  code: ({ className, children, ...props }) => {
    const inline = !className?.includes("language-");
    if (inline)
      return (
        <code className="rounded bg-surface-2 px-1 py-px font-mono text-[0.85em] text-text" {...props}>
          {children}
        </code>
      );
    return (
      <code className={cn("font-mono text-[0.82rem] leading-relaxed", className)} {...props}>
        {children}
      </code>
    );
  },
  pre: ({ children }) => {
    // ```chart fences render as real inline charts (Chart.js), not code blocks.
    const charts = React.Children.toArray(children).map(parseChartPayload);
    if (charts.length === 1 && charts[0]) {
      const c = charts[0];
      return (
        <ChartBlockComponent
          chart_type={c.chart_type}
          data={c.data}
          title={c.title}
          x_label={c.x_label}
          y_label={c.y_label}
        />
      );
    }
    return (
      <pre className="my-inline overflow-x-auto rounded-control border border-hairline bg-surface-1 px-body py-inline">
        {children}
      </pre>
    );
  },
  table: ({ children }) => (
    <div className="my-inline overflow-x-auto">
      <table className="w-full border-collapse text-[0.85rem]">{children}</table>
    </div>
  ),
  th: ({ children }) => <th className="border border-hairline bg-surface-1 px-inline py-hair text-left font-medium">{children}</th>,
  td: ({ children }) => <td className="border border-hairline px-inline py-hair">{children}</td>,
};

interface MarkdownProps {
  children: string;
  className?: string;
  /** Resolves [[id]] markers in prose/tables to Citation chips. Absent/null →
   *  the same neutral placeholder chip, never the raw marker text. */
  answer?: GroundedAnswer | null;
}

export function Markdown({ children, className, answer = null }: MarkdownProps) {
  const components = useMemo(() => {
    // Wrap the prose-bearing tags so [[id]] markers in their children render as
    // citation chips. This runs even before the final answer arrives (answer ===
    // null): CitationMarker then draws the same neutral, same-sized placeholder
    // CitedText always drew, so an in-flight block never leaks a raw "[[id]]" and
    // nothing shifts when provenance resolves (zero CLS).
    // Each override is listed explicitly (rather than assigned
    // through a `Components[keyof Components]` index) because that union is too
    // large for TS to represent on a computed-key assignment (TS2590).
    const wrap =
      (tag: "p" | "li" | "td" | "th" | "h1" | "h2" | "h3" | "blockquote") =>
      (props: { children?: React.ReactNode }) => {
        const Original = (COMPONENTS as Record<string, React.ElementType>)[tag];
        return <Original {...props}>{processCitations(props.children, answer)}</Original>;
      };
    return {
      ...COMPONENTS,
      p: wrap("p"),
      li: wrap("li"),
      td: wrap("td"),
      th: wrap("th"),
      h1: wrap("h1"),
      h2: wrap("h2"),
      h3: wrap("h3"),
      blockquote: wrap("blockquote"),
    } satisfies Components;
  }, [answer]);

  return (
    <div className={cn("text-text", className)}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {children}
      </ReactMarkdown>
    </div>
  );
}
