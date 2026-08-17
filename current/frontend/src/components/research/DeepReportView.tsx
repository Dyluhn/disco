/**
 * DeepReportView — the multi-section research report, in the document
 * paradigm. Reuses AnswerDocument's sticky-TOC two-column shape and the
 * existing CitedText parser for `[[passage_id]]` markers. Sections fade
 * in (pmx-rise) as they assemble. Each section carries a quiet confidence
 * indicator + optional disputed_notes callout — the engine's honesty
 * surfaces rendered subtly, not buried.
 */

import { AlertTriangle, CircleDot, Loader2 } from "lucide-react";
import { Markdown } from "@/components/Markdown";
import { CitedText } from "@/components/blocks";
import { ClaimVerdicts } from "@/components/research/ClaimVerdicts";
import { SupportMeter } from "@/components/research/SupportMeter";
import { asGroundedAnswer } from "@/components/research/groundedAnswer";
import { cn } from "@/lib/cn";
import type { AssemblingSection } from "@/lib/deepResearchTrace";
import type { GroundedAnswer, VerifiedClaim } from "@/types/grounded";
import type { ReportEvent } from "@/types/agent";

interface Props {
  query: string;
  summary: string | null;
  assembling: AssemblingSection[];
  /** When present, the finished report — supplies the authoritative passages
   * + all_hits used by CitedText to resolve [[id]] markers. */
  report: ReportEvent | null;
  /** Conversation id — threaded through so any block-level artifacts (sheet,
   *  slides, image) that DR emits can carry a working download.  ABSENT = no
   *  block download (no false affordance).  Currently DeepReportView renders
   *  prose-only sections; cid is accepted now so the wiring is in place when
   *  DR begins emitting block artifacts. */
  cid?: string | null;
}

/** Confidence mapping → existing verdict tokens. Quiet UI: dot + text, no
 *  alarm coloring on "high" (the default state), warn on "mixed", soft red
 *  on "low". Matches the chroma-on-meaning rule. */
const CONFIDENCE_VARIANT: Record<
  "high" | "mixed" | "low",
  { dot: string; label: string; tone: string }
> = {
  high: {
    dot: "bg-supported",
    label: "Sources align",
    tone: "text-text-muted",
  },
  mixed: {
    dot: "bg-weak",
    label: "Mixed support",
    tone: "text-weak",
  },
  low: {
    dot: "bg-unsupported",
    label: "Weak support",
    tone: "text-unsupported",
  },
};

/** REAL verdict counts from the report's per-claim NLI verdicts. These exist
 *  GLOBALLY (GroundedAnswer.claims) — a ReportSection carries only
 *  `unsupported_count`, NOT a per-claim breakdown, so the support meter is a
 *  report-level signal. We never fabricate a denominator (an earlier version
 *  used cited_passage_ids.length as a fake "total claims" — passages != claims). */
function claimVerdictCounts(claims: VerifiedClaim[]) {
  let supported = 0;
  let weak = 0;
  let unsupported = 0;
  for (const c of claims) {
    if (c.verdict === "unsupported") unsupported += 1;
    else if (c.verdict === "weak") weak += 1;
    else supported += 1;
  }
  return { supported, weak, unsupported };
}

function SectionSkeleton({ title, state }: { title: string; state: "pending" | "writing" }) {
  return (
    <section className="pmx-rise scroll-mt-24 opacity-80">
      <header className="mb-section flex items-center gap-inline border-b border-hairline pb-inline">
        <h2 className="font-display text-[1.4rem] font-medium leading-tight tracking-tight text-text">
          {title}
        </h2>
        <span
          className={cn(
            "ml-auto flex items-center gap-hair font-ui text-[0.72rem] uppercase tracking-wide text-text-faint",
          )}
        >
          {state === "writing" ? (
            <>
              <Loader2 className="size-3 animate-spin text-accent" aria-hidden />
              Writing
            </>
          ) : (
            <>
              <CircleDot className="size-3" aria-hidden />
              Pending
            </>
          )}
        </span>
      </header>
      <div className="space-y-inline">
        {[0, 1, 2].map((i) => (
          <div
            key={i}
            className={cn(
              "h-3 animate-pulse rounded-full bg-surface-2",
              i === 0 ? "w-11/12" : i === 1 ? "w-10/12" : "w-9/12",
            )}
          />
        ))}
      </div>
    </section>
  );
}

/** W-11: presentational variety. Every section used to render through ONE
 *  uniform template, so a whole report read as a flat wall of identical blocks.
 *  We classify each section by its POSITION + its CONTENT SHAPE — both derived
 *  from the real ReportSection, never fabricated — and vary only the chrome
 *  (container framing, an eyebrow label, prose emphasis). The body itself still
 *  renders through the same <Markdown>/<CitedText> pipeline, so citations and
 *  grounding are byte-for-byte unchanged; this is layout, not content. */
type SectionShape = "lead" | "figure" | "table" | "list" | "standard";

/** A GFM table needs a pipe row AND a dash-separator row; a chart is the
 *  fenced ```chart block the synthesis layer emits. BW-06: these are NOT the
 *  same — only a real chart earns the "Figure" eyebrow (a graph promise), while
 *  a table-only section is labeled "Table". Stamping "Figure" on a table was a
 *  false affordance (the reader expects a rendered graph that isn't there). */
function classifySection(markdown: string, index: number): SectionShape {
  // The opening section is the report's lead — give it a standfirst treatment.
  if (index === 0) return "lead";
  const hasChart = markdown.includes("```chart");
  const hasTable = /\n *\|.*\|/.test(markdown) && /\n *\|? *:?-{3,}/.test(markdown);
  if (hasChart) return "figure";
  if (hasTable) return "table";
  const lines = markdown
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);
  if (lines.length >= 2) {
    const listLines = lines.filter((l) => /^([-*+]|\d+[.)])\s+/.test(l)).length;
    if (listLines / lines.length >= 0.5) return "list";
  }
  return "standard";
}

/** Real Tailwind utilities + design tokens only (no invented classes that would
 *  silently do nothing). `body` uses an arbitrary-variant selector to style the
 *  lead standfirst / list markers without touching the markdown content. */
const SHAPE_CHROME: Record<
  SectionShape,
  { section?: string; eyebrow?: string; body?: string }
> = {
  lead: {
    section: "border-l-2 border-accent/50 pl-body",
    eyebrow: "Key finding",
    body: "[&>p:first-of-type]:text-[1.12rem] [&>p:first-of-type]:leading-relaxed [&>p:first-of-type]:text-text",
  },
  figure: {
    section: "rounded-card border border-hairline bg-surface-1/40 px-body py-section",
    eyebrow: "Figure",
  },
  table: {
    // Same card framing as a figure (it's still a data block worth surfacing),
    // but an honest "Table" label — no implied graph.
    section: "rounded-card border border-hairline bg-surface-1/40 px-body py-section",
    eyebrow: "Table",
  },
  list: {
    body: "[&_ul]:marker:text-accent [&_ol]:marker:text-accent",
  },
  standard: {},
};

function SectionView({
  section,
  answer,
  index,
}: {
  section: AssemblingSection;
  answer: GroundedAnswer | null;
  /** Position in the assembled report — drives the lead treatment + the
   *  alternating rhythm on plain sections. */
  index: number;
  /** Forwarded from DeepReportView — enables block-level downloads when
   *  DR emits sheet/slides/image artifacts (currently prose-only sections
   *  have no blocks to download; this prop is reserved for that future path.
   *  It is intentionally not destructured/read until BlockView calls appear
   *  here — the call site still forwards it so the wiring stays in place). */
  cid?: string | null;
}) {
  const real = section.section!;
  const conf = CONFIDENCE_VARIANT[real.confidence] ?? CONFIDENCE_VARIANT.high;
  const sectionUnsupported = real.unsupported_count ?? 0;
  const shape = classifySection(real.markdown, index);
  const chrome = SHAPE_CHROME[shape];
  // Plain sections get a faint left rule on odd positions so a long run of them
  // reads with rhythm instead of as one undifferentiated column.
  const altRule = shape === "standard" && index % 2 === 1 ? "border-l border-hairline pl-body" : "";
  return (
    <section
      id={section.id}
      className={cn("pmx-rise scroll-mt-24", chrome.section, altRule)}
    >
      <header className="mb-section border-b border-hairline pb-inline">
        {chrome.eyebrow && (
          <div className="mb-hair font-ui text-[0.66rem] font-semibold uppercase tracking-[0.12em] text-accent">
            {chrome.eyebrow}
          </div>
        )}
        <div className="flex flex-wrap items-baseline gap-inline">
          <h2 className="font-display text-[1.4rem] font-medium leading-tight tracking-tight text-text">
            {real.title}
          </h2>
          <span className="ml-auto flex shrink-0 items-center gap-section">
            {/* Per-section: only the HONEST datum the section carries —
                unsupported_count. No fabricated "supported total". */}
            {sectionUnsupported > 0 && (
              <span
                className="flex items-center gap-hair font-ui text-[0.72rem] uppercase tracking-wide text-unsupported"
                title="Claims in this section NLI could not verify against the sources"
              >
                <span className="inline-block size-1.5 rounded-full bg-unsupported" />
                {sectionUnsupported} unsupported
              </span>
            )}
            <span
              className={cn(
                "flex items-center gap-hair font-ui text-[0.72rem] uppercase tracking-wide",
                conf.tone,
              )}
              title={`Per-claim NLI verification: ${real.confidence}`}
            >
              <span className={cn("inline-block size-1.5 rounded-full", conf.dot)} />
              {conf.label}
            </span>
          </span>
        </div>
      </header>

      {real.disputed_notes && real.disputed_notes.length > 0 && (
        <aside
          role="note"
          className="mb-section flex items-start gap-inline rounded-card border border-weak/40 bg-surface-1 px-body py-inline"
        >
          <AlertTriangle className="mt-px size-3.5 shrink-0 text-weak" aria-hidden />
          <div className="font-ui text-[0.84rem] leading-snug text-text-muted">
            <span className="font-medium text-weak">Sources disagree.</span>{" "}
            {/* Render through CitedText so [[passage_id]] markers resolve
                to citation chips instead of leaking as raw text. */}
            <CitedText text={real.disputed_notes.join(" ")} answer={answer} />
          </div>
        </aside>
      )}

      <div className={cn("prose-reading", chrome.body)}>
        <Markdown answer={answer}>{real.markdown}</Markdown>
      </div>

      <ClaimVerdicts
        claims={answer?.claims ?? []}
        passages={answer?.passages ?? []}
      />
    </section>
  );
}

export function DeepReportView({ query, summary, assembling, report, cid }: Props) {
  const answer = asGroundedAnswer(report, query);
  // ToC: every section title with a state badge (writing / pending get an
  // indicator beside the link so the user knows they're in flight).
  return (
    <div className="mx-auto grid w-full max-w-doc grid-cols-1 gap-major px-body lg:grid-cols-[1fr_14rem]">
      <article className="mx-auto flex w-full max-w-measure flex-col gap-section">
        <header className="flex flex-wrap items-baseline gap-inline border-b border-hairline pb-section">
          <h1 className="font-display text-[2.25rem] font-medium leading-tight tracking-tight text-text">
            {query}
          </h1>
          {/* Report-level NLI support signal — REAL per-claim verdict counts. */}
          {answer && answer.claims.length > 0 && (
            <span className="ml-auto shrink-0 self-center">
              <SupportMeter {...claimVerdictCounts(answer.claims)} />
            </span>
          )}
        </header>

        {summary && (
          <section className="pmx-rise">
            <header className="mb-section flex items-baseline gap-inline border-b border-hairline pb-inline">
              <h2 className="font-display text-[1.2rem] font-medium leading-tight tracking-tight text-text-muted">
                Executive summary
              </h2>
            </header>
            <div className="prose-reading">
              <Markdown answer={answer}>{summary}</Markdown>
            </div>
          </section>
        )}

        {assembling.map((s, i) =>
          s.state === "done" && s.section ? (
            <SectionView key={s.id} section={s} answer={answer} index={i} cid={cid} />
          ) : (
            // A section reaches "done" only once its ReportSection is present
            // (deriveAssemblingSections), so here state is "pending" | "writing".
            <SectionSkeleton
              key={s.id}
              title={s.title}
              state={s.state === "writing" ? "writing" : "pending"}
            />
          ),
        )}
      </article>

      {assembling.length > 0 && (
        <nav
          aria-label="Report sections"
          className="hidden self-start lg:sticky lg:top-section lg:block"
        >
          <div className="mb-inline font-ui text-[0.7rem] font-semibold uppercase tracking-wide text-text-faint">
            Sections
          </div>
          <ul className="flex flex-col gap-hair border-l border-hairline">
            {summary && (
              <li>
                <a
                  href={`#summary`}
                  className="-ml-px block border-l border-transparent py-hair pl-body font-ui text-[0.82rem] text-text-muted transition-colors hover:border-accent hover:text-text"
                >
                  Executive summary
                </a>
              </li>
            )}
            {assembling.map((s) => (
              <li key={s.id}>
                <a
                  href={`#${s.id}`}
                  className={cn(
                    "-ml-px block border-l border-transparent py-hair pl-body font-ui text-[0.82rem] transition-colors hover:border-accent hover:text-text",
                    s.state === "done" ? "text-text-muted" : "text-text-faint",
                  )}
                >
                  {s.title}
                  {s.state === "writing" && (
                    <Loader2
                      className="ml-hair inline size-3 animate-spin text-accent align-text-bottom"
                      aria-hidden
                    />
                  )}
                </a>
              </li>
            ))}
          </ul>
        </nav>
      )}
    </div>
  );
}
