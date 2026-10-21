import { Fragment, type ReactNode } from "react";
import { citationNumbers, passageById } from "@/lib/sources";
import type { GroundedAnswer, Verdict } from "@/types/grounded";
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

/** Formatting is presentation-only; punctuation inside claims (e.g.
 * example.com) is retained. This binds recorded text, never infers support. */
function claimText(text: string): string {
  return text
    .replace(/\[\[\w+\]\]/g, "")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/^[ \t]*(?:[-*+] |\d+[.)] |#{1,6} |> )/gm, "")
    .replace(/[*_`~]/g, "")
    .replace(/\s+/g, " ")
    .replace(/ +([,;:.!?])/g, "$1");
}

function occurrenceVerdict(answer: GroundedAnswer, id: string, textBefore: string, textAfter: string): Verdict {
  const before = claimText(textBefore).trimStart();
  const prose = claimText(`${textBefore} ${textAfter}`).trimStart();
  const marker = before.length;
  const matches = answer.claims.filter(({ claim }) => {
    if (!claim.cited_passage_ids.includes(id)) return false;
    const text = claimText(claim.text).trim().replace(/[.,;:!?]+$/, "").trim();
    if (!text) return false;
    for (let start = prose.indexOf(text); start >= 0; start = prose.indexOf(text, start + 1)) {
      const prefix = prose.slice(0, start).trimEnd();
      // A shorter positive claim must not match "not <claim>" or another
      // qualification inside a sentence merely because its suffix is equal.
      if (prefix && !/[.!?]$/.test(prefix)) continue;
      const end = start + text.length;
      if (marker < start) continue;
      // A citation may follow its claim or be embedded inside the verified
      // sentence. Include its remaining qualification, never just the prefix.
      if (marker <= end || /^[\s.,;:!?]*$/.test(prose.slice(end, marker))) return true;
    }
    return false;
  });
  // No positional identity exists for identical repeated claims. An ambiguous
  // or unmatched occurrence must never inherit another claim's green verdict.
  if (matches.length !== 1) return "weak";
  return matches[0].verdict;
}

/** Individual citation marker (the [n] chip or a placeholder). */
export function CitationMarker({ id, answer, textBefore = "", textAfter = "" }: {
  id: string;
  answer: GroundedAnswer | null;
  /** Prose before this exact marker, including earlier markers in its group. */
  textBefore?: string;
  /** Remaining prose, including any qualification after an inline marker. */
  textAfter?: string;
}) {
  const numbers = answer ? citationNumbers(answer) : null;
  const passage = answer ? passageById(answer, id) : undefined;
  if (answer && passage && numbers) {
    return (
      <Citation
        n={numbers.get(id) ?? 0}
        passage={passage}
        verdict={occurrenceVerdict(answer, id, textBefore, textAfter)}
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
    out.push(<CitationMarker key={key++} id={id} answer={answer} textBefore={text.slice(0, m.index)} textAfter={text.slice(m.index + m[0].length)} />);
    last = m.index + m[0].length;
  }
  out.push(<Fragment key={key++}>{inlineMd(text.slice(last), key * 1000)}</Fragment>);
  return <>{out}</>;
}
