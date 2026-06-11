/**
 * DeepReportView — the multi-section research report, in the document
 * paradigm. Reuses AnswerDocument's sticky-TOC two-column shape and the
 * existing CitedText parser for `[[passage_id]]` markers. Sections fade
 * in (pmx-rise) as they assemble. Each section carries a quiet confidence
 * indicator + optional disputed_notes callout — the engine's honesty
 * surfaces rendered subtly, not buried.
 */

import { AlertTriangle, CircleDot, Loader2 } from "lucide-react";
import { CitedText } from "@/components/blocks";
import { Markdown } from "@/components/Markdown";
import { cn } from "@/lib/cn";
import type { AssemblingSection } from "@/lib/deepResearchTrace";
import type { GroundedAnswer, Passage, SearchHit } from "@/types/grounded";
import type { ReportEvent } from "@/types/agent";

interface Props {
  query: string;
  summary: string | null;
  assembling: AssemblingSection[];
  /** When present, the finished report — supplies the authoritative passages
   * + all_hits used by CitedText to resolve [[id]] markers. */
  report: ReportEvent | null;
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

/** Cast report.passages + report.all_hits into the typed shapes CitedText
 * resolves. They're plain dicts on the event wire (kept core-free of
 * retrieval imports); the cast lives at the render boundary. */
function asGroundedAnswer(report: ReportEvent | null, query: string): GroundedAnswer | null {
  if (!report) return null;
  return {
    query,
    blocks: [],
    claims: [],
    passages: report.passages as unknown as Passage[],
    all_hits: report.all_hits as unknown as SearchHit[],
    unsupported_count: report.unsupported_count,
    follow_ups: [],
  };
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

function SectionView({
  section,
  answer,
}: {
  section: AssemblingSection;
  answer: GroundedAnswer | null;
}) {
  const real = section.section!;
  const conf = CONFIDENCE_VARIANT[real.confidence] ?? CONFIDENCE_VARIANT.high;
  return (
    <section id={section.id} className="pmx-rise scroll-mt-24">
      <header className="mb-section flex flex-wrap items-baseline gap-inline border-b border-hairline pb-inline">
        <h2 className="font-display text-[1.4rem] font-medium leading-tight tracking-tight text-text">
          {real.title}
        </h2>
        <span
          className={cn(
            "ml-auto flex shrink-0 items-center gap-hair font-ui text-[0.72rem] uppercase tracking-wide",
            conf.tone,
          )}
          title={`Per-claim NLI verification: ${real.confidence}`}
        >
          <span className={cn("inline-block size-1.5 rounded-full", conf.dot)} />
          {conf.label}
        </span>
      </header>

      {real.disputed_notes && real.disputed_notes.length > 0 && (
        <aside
          role="note"
          className="mb-section flex items-start gap-inline rounded-card border border-weak/40 bg-surface-1 px-body py-inline"
        >
          <AlertTriangle className="mt-px size-3.5 shrink-0 text-weak" aria-hidden />
          <div className="font-ui text-[0.84rem] leading-snug text-text-muted">
            <span className="font-medium text-weak">Sources disagree.</span>{" "}
            {real.disputed_notes.join(" ")}
          </div>
        </aside>
      )}

      <div className="prose-reading">
        <Markdown answer={answer}>{real.markdown}</Markdown>
      </div>
    </section>
  );
}

export function DeepReportView({ query, summary, assembling, report }: Props) {
  const answer = asGroundedAnswer(report, query);
  // ToC: every section title with a state badge (writing / pending get an
  // indicator beside the link so the user knows they're in flight).
  return (
    <div className="mx-auto grid w-full max-w-doc grid-cols-1 gap-major px-body lg:grid-cols-[1fr_14rem]">
      <article className="mx-auto flex w-full max-w-measure flex-col gap-section">
        <header className="border-b border-hairline pb-section">
          <h1 className="font-display text-[2.25rem] font-medium leading-tight tracking-tight text-text">
            {query}
          </h1>
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

        {assembling.map((s) =>
          s.state === "done" && s.section ? (
            <SectionView key={s.id} section={s} answer={answer} />
          ) : (
            <SectionSkeleton key={s.id} title={s.title} state={s.state} />
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
