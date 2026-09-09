/**
 * DeepBoundedNotice — the honest "what was cut" surface. A quiet aside outside
 * the document naming what this run did not close: the research budget that
 * ended the evidence loop (`bounded_by`), any sentence the grounding verifier
 * could not support against the gathered evidence when the report was
 * published (`meta.unverified_sentences`), and a model review that was
 * unavailable for the run (`meta.review_outcome`). The report itself remains
 * a finished artifact — a bound is a budget that was reached, and an
 * unverified sentence is a declared one, never an incomplete checklist. The
 * system never edits the prose; it says what it could not check.
 *
 * Most tools hide this — a run "completes" and you're left guessing what's
 * been left out. Surfacing it elegantly is the trust move that fits this
 * product's discipline.
 *
 * L24: "bounded by the turn budget" was doing double duty. A run that spent
 * every turn on real work and a run whose turns evaporated against a search or
 * extraction outage produced the identical sentence, and only the second
 * is worth re-running. When the engine records the ledger
 * (`meta.turn_accounting`) the notice now says which one happened; without it
 * the generic sentence stands alone rather than guessing.
 */

import { Info, Layers, Zap } from "lucide-react";
import { countOf } from "@/lib/deepResearchHeartbeat";
import { UNRESOLVED_MEANING } from "@/lib/claimCheck";
import { DEPTH_TIER_LABEL, depthTierLabel } from "@/lib/depthTier";
import type { ReportEvent } from "@/types/agent";

interface Props {
  report: ReportEvent;
  onTryExhaustive?: () => void;
}

/**
 * Sentences the verifier could not ground in the evidence, recorded verbatim by
 * the engine. Reports persisted before the writer rewrite carry the same idea
 * as `meta.residual_deficiencies` (review findings left open) — still read,
 * never emitted by a new run.
 */
export function unverifiedSentences(report: ReportEvent): string[] {
  const raw = report.meta?.unverified_sentences ?? report.meta?.residual_deficiencies;
  if (!Array.isArray(raw)) return [];
  return raw.filter((item): item is string => typeof item === "string" && item.trim() !== "");
}

/** Whether the run's one model review never returned a verdict. */
export function reviewUnavailable(report: ReportEvent): boolean {
  return report.meta?.review_outcome === "unavailable";
}

/** A stored sentence keeps its `[[passage-id]]` markers; the aside reads as prose. */
function withoutCitationMarkers(sentence: string): string {
  return sentence
    .replace(/\s*\[\[[\w-]+\]\]/g, "")
    .replace(/\s+([.,;:!?])/g, "$1")
    .trim();
}

/**
 * The run's turn ledger, when the engine recorded one on the report:
 * `meta.turn_accounting = {model_turns, degraded_turns, refused_turns, of}`.
 *
 * "Bounded by the turn budget" means three very different things and the notice
 * used to blur them: the model genuinely spent every turn on productive work,
 * the turns evaporated against a search or extraction outage, or the turns went to
 * proposals the host's query walls refused so nothing was ever searched. Null
 * when the backend did not record the ledger — the notice then falls back to
 * the generic form rather than guessing.
 *
 * `refused_turns` arrived after `model_turns`/`degraded_turns`, so a report
 * written before it reads as 0 and renders exactly as it did.
 */
export function turnAccounting(
  report: ReportEvent,
): { modelTurns: number; degradedTurns: number; refusedTurns: number; inspectionTurns: number; of: number } | null {
  const raw = report.meta?.turn_accounting;
  if (typeof raw !== "object" || raw === null) return null;
  const row = raw as Record<string, unknown>;
  const modelTurns = Number(row.model_turns);
  const degradedTurns = Number(row.degraded_turns);
  const refusedTurns = Number(row.refused_turns ?? 0);
  const inspectionTurns = Number(row.inspection_turns ?? 0);
  const of = Number(row.of);
  if (![modelTurns, degradedTurns, of].every((n) => Number.isFinite(n))) return null;
  return {
    modelTurns,
    degradedTurns,
    refusedTurns: Number.isFinite(refusedTurns) ? refusedTurns : 0,
    inspectionTurns: Number.isInteger(inspectionTurns) && inspectionTurns >= 0 ? inspectionTurns : 0,
    of,
  };
}

/**
 * What the ledger RECORDED, in the ledger's own numbers.
 *
 * The previous wording said "All {of} research turns …" — but `of` is the
 * BUDGET, not the turns spent. S1 caught it on a real report whose ledger read
 * `{model_turns: 7, degraded_turns: 0, of: 8}`: the notice claimed 8 turns went
 * to productive searches when the engine had recorded 7. Turns spent is
 * `model_turns + degraded_turns`; every number below is one of those or their
 * sum, and nothing is rounded up to the budget.
 *
 * T3: `degraded_turns === 0` then made it claim every turn "went to a search
 * that came back with results" — a few lines under a trace reading
 * "Not searched ×3". A turn whose every proposed query the host had already run
 * issues nothing and is still charged to the model, so it gets said out loud.
 */
function turnTruth(ledger: {
  modelTurns: number;
  degradedTurns: number;
  refusedTurns: number;
  inspectionTurns: number;
  of: number;
}): string {
  const spent = ledger.modelTurns + ledger.degradedTurns;
  const head = `The run recorded ${countOf(spent, "research turn")} of its ${ledger.of}`;
  const refused =
    ledger.refusedTurns > 0
      ? ` ${countOf(ledger.refusedTurns, "turn")} issued no search at all: every query proposed on ${ledger.refusedTurns === 1 ? "it had" : "them had"} already been run.`
      : "";
  if (ledger.inspectionTurns > 0) {
    const degraded = ledger.degradedTurns > 0
      ? ` ${countOf(ledger.degradedTurns, "turn")} returned no usable search result because searching or reading pages failed.`
      : "";
    return `${head}. ${countOf(ledger.inspectionTurns, "turn")} requested a closer reading of already gathered sources.${refused}${degraded}`;
  }
  if (ledger.degradedTurns <= 0) {
    if (ledger.refusedTurns > 0) return `${head}.${refused}`;
    return `${head}, and every one went to a search that came back with results.`;
  }
  // F7: this used to say "because search engines were rate-limited". The ledger
  // counts a turn as degraded when its every query ended in ANY infrastructure
  // outcome — a degraded search provider, a transport failure, OR an extraction
  // failure (pages found, none readable). On the T3 pass-3 report the one
  // degraded turn was an extraction failure and the notice named a rate limit
  // that never happened, a few lines under a trace that now says which it was.
  return `${head}; ${ledger.degradedTurns} of those came back empty because searching or reading the pages failed, not because the subject ran out.${refused} Running this again while the search pool is healthy will get further than a bigger budget will.`;
}

/** Whether this report has anything honest to disclose outside the document. */
export function hasReportDisclosure(report: ReportEvent): boolean {
  const bounded = Boolean(report.bounded_by) && report.bounded_by !== "rounds";
  return bounded || unverifiedSentences(report).length > 0 || reviewUnavailable(report)
    || report.meta?.review_outcome === "incomplete"
    || groundingCounts(report) !== null || reviewNotes(report).length > 0;
}

const BOUND_TEXT: Record<string, { what: string; lever: string }> = {
  sources: {
    what: "the web-source budget",
    lever:
      "Each tier caps newly gathered web passages. Additional retrieval stopped at that cap; inspection of retained sources can continue within the remaining research turns.",
  },
  turns: {
    what: "the research-turn budget",
    lever:
      "Each tier caps how many research turns the model may spend. Further searching stopped when this run reached that cap.",
  },
  // Legacy: runs recorded before the research budget became work-denominated
  // persisted "wall_clock". Still rendered, never emitted by a new run.
  wall_clock: {
    what: "the research budget",
    lever:
      "The run reached its research budget before the model called the evidence sufficient.",
  },
};

/** Which budget closed the loop, plus — when the engine recorded it — whether
 *  the turns went to real work or evaporated against an infrastructure outage. */
function BoundExplanation({ report }: { report: ReportEvent }) {
  const text =
    (report.bounded_by ? BOUND_TEXT[report.bounded_by] : undefined) ??
    { what: report.bounded_by ?? "", lever: "" };
  const ledger = turnAccounting(report);
  return (
    <>
      <p className="mt-hair font-ui text-[0.82rem] leading-snug text-text-muted">
        This run was bounded by <span className="text-text">{text.what}</span>. {text.lever}
      </p>
      {/* Only when the engine actually recorded the ledger. Without it the
          generic sentence above stands alone rather than guessing which kind
          of bound this was. */}
      {ledger && (
        <p className="mt-hair font-ui text-[0.82rem] leading-snug text-text-muted">
          {turnTruth(ledger)}
        </p>
      )}
    </>
  );
}

/** Sentences the verifier could not ground, verbatim minus citation markup. */
function UnverifiedList({ sentences }: { sentences: string[] }) {
  if (sentences.length === 0) return null;
  return (
    <details className="mt-hair">
      <summary className="cursor-pointer font-ui text-[0.82rem] leading-snug text-text-muted">
        {sentences.length === 1
          ? "One sentence could not be verified against the gathered evidence:"
          : `${sentences.length} sentences could not be verified against the gathered evidence:`}
      </summary>
      <ul className="mt-hair list-disc space-y-hair pl-5 font-ui text-[0.8rem] leading-snug text-text-muted">
        {sentences.map((sentence) => (
          <li key={sentence}>{withoutCitationMarkers(sentence)}</li>
        ))}
      </ul>
    </details>
  );
}

/** The one model review returned no verdict; only the system's own checks ran. */
function ReviewUnavailableNote() {
  return (
    <p className="mt-hair font-ui text-[0.82rem] leading-snug text-text-muted">
      The model's editorial review was unavailable for this run.
    </p>
  );
}

function groundingCounts(report: ReportEvent): Record<string, number> | null {
  const raw = report.meta?.grounding_counts;
  if (typeof raw !== "object" || raw === null) return null;
  const row = raw as Record<string, unknown>;
  const keys = ["supported", "contradicted", "unresolved", "unavailable"];
  if (!keys.every((key) => typeof row[key] === "number" && Number.isInteger(row[key]) && Number(row[key]) >= 0)) {
    return null;
  }
  return Object.fromEntries(keys.map((key) => [key, Number(row[key])]));
}

function reviewNotes(report: ReportEvent): string[] {
  const raw = report.meta?.review_notes;
  return Array.isArray(raw)
    ? raw.filter((item): item is string => typeof item === "string" && item.trim() !== "")
    : [];
}

export function DeepBoundedNotice({ report, onTryExhaustive }: Props) {
  // "rounds" is the per-round DEPTH cap, not a coverage truncation, so it is
  // not surfaced (mirrors the backend report_truncation() shared by the
  // PDF/markdown/LLM-context exporters).
  const bounded = Boolean(report.bounded_by) && report.bounded_by !== "rounds";
  const sentences = unverifiedSentences(report);
  const noReview = reviewUnavailable(report);
  const incompleteReview = report.meta?.review_outcome === "incomplete";
  const counts = groundingCounts(report);
  const notes = reviewNotes(report);
  if (!hasReportDisclosure(report)) return null;

  return (
    <aside
      role="note"
      aria-label="What this run did not close"
      className="rounded-card border border-hairline bg-surface-1 px-body py-body"
    >
      <div className="flex items-start gap-inline">
        <Layers
          className="mt-px size-4 shrink-0 text-text-faint"
          aria-hidden
        />
        <div className="flex-1">
          <div className="font-ui text-[0.84rem] font-medium text-text">
            Report compiled from the strongest evidence gathered
          </div>
          {bounded && <BoundExplanation report={report} />}

          {noReview && <ReviewUnavailableNote />}
          {incompleteReview && (
            <p className="mt-hair font-ui text-[0.82rem] leading-snug text-text-muted">
              Review found unresolved issues in this report. See the findings below.
            </p>
          )}
          {counts && (
            <>
              <p className="mt-hair font-ui text-[0.82rem] leading-snug text-text-muted">
                Automated evidence check: {counts.supported} supported, {counts.contradicted} possible contradictions,
                {" "}{counts.unresolved} unresolved, {counts.unavailable} not checked.
              </p>
              {/* Without this line the counts read as "three quarters of this
                  report is unsupported" — which is not what the checker
                  measured (UI-20). */}
              <p
                data-dr-unresolved-meaning=""
                className="mt-hair font-ui text-[0.78rem] leading-snug text-text-faint"
              >
                {UNRESOLVED_MEANING}
              </p>
            </>
          )}
          {notes.map((note, index) => (
            <p key={index} className="mt-hair font-ui text-[0.82rem] leading-snug text-text-muted">{note}</p>
          ))}
          <UnverifiedList sentences={sentences} />

          <div className="mt-body flex flex-wrap items-center gap-inline">
            {report.depth_tier && (
              <span className="flex items-center gap-hair rounded-full border border-hairline px-inline py-px font-ui text-[0.72rem] uppercase tracking-wide text-text-faint">
                <Info className="size-2.5" aria-hidden />
                Tier: {depthTierLabel(report.depth_tier)}
              </span>
            )}
            {/* A bigger research budget only answers a BUDGET bound. Offering
                it for an unverified sentence would be a false affordance. */}
            {bounded && onTryExhaustive && report.depth_tier !== "exhaustive" && (
              <button
                type="button"
                onClick={onTryExhaustive}
                data-disco-control="dr.retry-exhaustive"
                className="flex min-h-11 items-center gap-hair rounded-control border border-accent/40 px-inline py-hair font-ui text-[0.78rem] text-accent transition-colors hover:bg-accent hover:text-bg lg:min-h-0"
              >
                <Zap className="size-3" aria-hidden />
                Run on {DEPTH_TIER_LABEL.exhaustive} tier
              </button>
            )}
          </div>
        </div>
      </div>
    </aside>
  );
}
