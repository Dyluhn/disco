/**
 * Markdown → token-styled elements. The agent's messages are free-form markdown (bold,
 * inline + fenced code, lists, links, tables); rendering them literally was the bug. This
 * maps every element to the design tokens so it reads as part of the product, dark + light.
 */

import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/cn";

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
  pre: ({ children }) => (
    <pre className="my-inline overflow-x-auto rounded-control border border-hairline bg-surface-1 px-body py-inline">
      {children}
    </pre>
  ),
  table: ({ children }) => (
    <div className="my-inline overflow-x-auto">
      <table className="w-full border-collapse text-[0.85rem]">{children}</table>
    </div>
  ),
  th: ({ children }) => <th className="border border-hairline bg-surface-1 px-inline py-hair text-left font-medium">{children}</th>,
  td: ({ children }) => <td className="border border-hairline px-inline py-hair">{children}</td>,
};

export function Markdown({ children, className }: { children: string; className?: string }) {
  return (
    <div className={cn("text-text", className)}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={COMPONENTS}>
        {children}
      </ReactMarkdown>
    </div>
  );
}
