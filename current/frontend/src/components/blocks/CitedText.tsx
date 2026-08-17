import { Fragment, type ReactNode } from "react";
import { citationNumbers, passageById, verdictForPassage } from "@/lib/sources";
import type { GroundedAnswer } from "@/types/grounded";
import { Citation } from "../Citation";

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
