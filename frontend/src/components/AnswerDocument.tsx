import type { AnswerBlock, GroundedAnswer } from "@/types/grounded";
import { Markdown } from "@/components/Markdown";
import { splitThink } from "@/lib/think";
import { BlockView } from "./blocks";

interface Props {
  blocks: AnswerBlock[];
  partial: Record<string, string>;
  streamingBlockId: string | null;
  answer: GroundedAnswer | null;
  /** Conversation id (cid) — threaded down to SheetBlock so the in-block
   *  download appears ONLY when the caller holds a real cid. ABSENT = no
   *  download button (no false affordance). */
  cid?: string | null;
}

/** The answer as a document (not a chat column): a sticky in-page ToC beside a
 * comfortable reading measure. Completed blocks render via BlockView; the
 * in-flight block renders its live token text in a rigid container so nothing
 * below shifts (append-only growth, never reflow of existing content). */
export function AnswerDocument({
  blocks,
  partial,
  streamingBlockId,
  answer,
  cid,
}: Props) {
  const headings = blocks.filter((b) => b.kind === "heading");
  const streamingText =
    streamingBlockId && !blocks.some((b) => b.id === streamingBlockId)
      ? partial[streamingBlockId]
      : undefined;

  return (
    <div className="mx-auto grid w-full max-w-doc grid-cols-1 gap-major px-body lg:grid-cols-[1fr_14rem]">
      <article className="mx-auto flex w-full max-w-measure flex-col gap-section">
        {blocks.map((block) => (
          <div key={block.id} className="pmx-rise">
            <BlockView block={block} answer={answer} cid={cid} />
          </div>
        ))}
        {streamingText !== undefined && (
          <div className="prose-reading">
            <Markdown>{splitThink(streamingText).answer}</Markdown>
            <span className="ml-px inline-block h-[1.1em] w-[2px] translate-y-[0.15em] animate-pulse bg-accent align-middle" />
          </div>
        )}
      </article>

      {headings.length > 0 && (
        <nav
          aria-label="On this page"
          className="hidden self-start lg:sticky lg:top-section lg:block"
        >
          <div className="mb-inline font-ui text-[0.7rem] font-semibold uppercase tracking-wide text-text-faint">
            On this page
          </div>
          <ul className="flex flex-col gap-hair border-l border-hairline">
            {headings.map((h) => (
              <li key={h.id}>
                <a
                  href={`#${h.id}`}
                  className="-ml-px block border-l border-transparent py-hair pl-body font-ui text-[0.82rem] text-text-muted transition-colors hover:border-accent hover:text-text"
                >
                  {h.kind === "heading" ? h.text : ""}
                </a>
              </li>
            ))}
          </ul>
        </nav>
      )}
    </div>
  );
}
