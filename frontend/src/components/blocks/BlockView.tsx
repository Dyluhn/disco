import { cn } from "@/lib/cn";
import type { AnswerBlock, GroundedAnswer } from "@/types/grounded";
import { CitedText } from "./CitedText";
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
    case "prose":
      return (
        <p className="prose-reading">
          <CitedText text={block.text} answer={answer} />
        </p>
      );
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
