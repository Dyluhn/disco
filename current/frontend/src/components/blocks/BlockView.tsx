import { cn } from "@/lib/cn";
import { Markdown } from "@/components/Markdown";
import { splitThink } from "@/lib/think";
import type { AnswerBlock, GroundedAnswer } from "@/types/grounded";
import { CodeBlock } from "./CodeBlock";
import { TableView } from "./TableView";
import { ChartBlockComponent } from "./ChartBlock";
import { SheetBlockComponent } from "./SheetBlock";
import { SlidesBlockComponent } from "./SlidesBlock";

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
    case "prose": {
      // MiniMax-class drivers can leak an inline <think>…</think> preamble into
      // the answer text; reasoning is never part of the rendered document.
      const prose = splitThink(block.text).answer || block.text;
      // UI-39: prose arrives as MARKDOWN (bullet lists, headings, tables), so it
      // renders through the same <Markdown> pipeline the deep-research report view
      // uses. Rendering it as one inline text run collapsed every "- item" line into
      // a single paragraph with literal " - " separators. <Markdown> resolves the
      // [[passage_id]] markers itself, so citation chips are unchanged.
      return (
        <div className="prose-reading">
          <Markdown answer={answer}>{prose}</Markdown>
        </div>
      );
    }
    case "code":
      return <CodeBlock language={block.language} code={block.code} />;
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
