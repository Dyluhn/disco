/**
 * DeepResearchReportView — the report document (assembles while RUNNING,
 * settles at FINISHED), the 3-tier sources panel, the follow-up Q&A thread,
 * the follow-up activity indicator, the "Need More?" card, and the settings
 * footer. Relocated verbatim from DeepResearchSurface.tsx's `started` JSX.
 * See that file's header for the surface map.
 */
import { Settings as SettingsIcon } from "lucide-react";
import { Link } from "react-router-dom";
import { Markdown } from "@/components/Markdown";
import type { useDeepResearch } from "@/hooks/useDeepResearch";
import { computeFollowUpGrounding } from "@/lib/deepResearchTraceParts/activity";
import type { AgentEvent, ResearchFollowUpGroundingPayload } from "@/types/agent";
import { DeepReportView } from "../DeepReportView";
import { asGroundedAnswer } from "../groundedAnswer";
import { TieredSourcePanel } from "../TieredSourcePanel";
import { NeedMoreCard } from "../NeedMoreCard";
import { FollowUpStatus } from "../FollowUpStatus";

interface Props {
  r: ReturnType<typeof useDeepResearch>;
}

/**
 * The grounding ledger the server appended immediately before ONE answer.
 *
 * `computeFollowUpGrounding` reads the newest ledger in the events it is given,
 * so a thread with several follow-ups needs the log cut at the answer being
 * rendered — otherwise every answer would show the last one's counts.
 */
function groundingFor(
  events: AgentEvent[],
  answerSeq: number | null | undefined,
): ResearchFollowUpGroundingPayload | null {
  if (answerSeq === null || answerSeq === undefined) return null;
  return computeFollowUpGrounding(events.filter((e) => (e.seq ?? 0) <= answerSeq));
}

/**
 * One follow-up answer, with the server's own count of what survived grounding.
 *
 * Two shapes, and the ledger picks which: an ANSWER carries the tally under it,
 * so a reader can see that one of eleven statements went in only partly
 * supported without having to trust the prose; a REFUSAL is a wall and is drawn
 * as one — the server already wrote its four parts (why / state now / next /
 * what is still allowed), so the text is rendered verbatim and only the frame
 * around it says which kind of thing this is. A wall's tally is 0 · 0 · 0 and
 * tells the reader nothing, so it is not shown.
 */
function FollowUpAnswer({
  content,
  grounding,
  answer,
}: {
  content: string;
  grounding: ResearchFollowUpGroundingPayload | null;
  answer: ReturnType<typeof asGroundedAnswer>;
}) {
  const refused = grounding?.refused === true;
  return (
    <div
      data-dr-followup={refused ? "wall" : "answer"}
      className={`rounded-card border bg-surface-1 px-body py-body ${
        refused ? "border-hairline-strong" : "border-hairline"
      }`}
    >
      <p className="mb-inline font-ui text-[0.72rem] uppercase tracking-wide text-text-faint">
        {refused ? "Not answered" : "Answer"}
      </p>
      {/* Render through <Markdown> so the model's markdown (bold, lists, code,
          etc.) formats properly. Pass the report's passage corpus so [[id]]
          citation markers resolve to chips instead of staying as raw
          brackets. */}
      <div className="prose-reading">
        <Markdown answer={answer}>{content}</Markdown>
      </div>
      {grounding !== null && !refused && (
        <p
          data-dr-followup-grounding=""
          className="mt-inline font-ui text-[0.74rem] text-text-faint"
        >
          {grounding.supported} supported · {grounding.weak} partly supported ·{" "}
          {grounding.removed} removed
        </p>
      )}
    </div>
  );
}

export function DeepResearchReportView({ r }: Props) {
  return (
    <>
      {/* The report document. v2 (gateless): there is no plan to wait for, so
          the mount condition is the report itself — sections are written in
          one pass and arrive with the ReportEvent. Rendering the shell before
          then would be an empty document promising content that doesn't
          exist yet; the progress strip owns the in-flight story. */}
      {r.report && (
        <DeepReportView
          query={r.query ?? ""}
          titleAlreadyShown
          summary={r.report.summary ?? null}
          assembling={r.assembling}
          report={r.report}
          cid={r.cid}
        />
      )}

      {/* The 3-tier sources panel — only after the report arrives. */}
      {r.report && (
        <div className="mx-auto w-full max-w-doc">
          <TieredSourcePanel tiers={r.sources} />
        </div>
      )}

      {/* Wave 2 — follow-up Q&A. The RP-13 follow-up path appends the user's
          question + the agent's grounded answer as MessageEvents after the
          report; DeepReportView only renders the ReportEvent, so these were
          generated server-side but never shown (the answer vanished). Render
          them here as a thread. Plumbing messages are already suppressed in
          the hook's `followUps` derivation (whitelist boundary).
          Gate on report + FINISHED (WALK-08 A4): before the report exists,
          reportSeq would be -1 and the run's own initiating message would
          be misclassified as a follow-up. This gate prevents that ghost. */}
      {r.report && r.status === "FINISHED" && r.followUps.length > 0 && (
        <div className="mx-auto w-full max-w-doc space-y-section">
          {r.followUps.map((m, i) =>
            m.message.role === "user" ? (
              <p key={i} className="font-ui text-[0.95rem] font-medium text-text">
                <span className="text-text-faint">Follow-up: </span>
                {m.message.content}
              </p>
            ) : (
              <FollowUpAnswer
                key={i}
                content={m.message.content}
                grounding={groundingFor(r.events, m.seq)}
                answer={asGroundedAnswer(r.report, r.query ?? "")}
              />
            ),
          )}
        </div>
      )}

      {/* WALK-12: follow-up activity indicator. Shows "Reading sources…" /
          "Writing answer…" while a follow-up is generating so the UI
          doesn't look frozen. Driven by followUpStatus (WALK-11) + the
          phase ActionEvents the backend emits during _follow_up_deep_research.
          Rendered BELOW the Q&A thread and ABOVE the NeedMoreCard. */}
      <FollowUpStatus
        followUpStatus={r.followUpStatus}
        events={r.events}
      />

      {/* "Need More?" card — Ask a Follow-Up / Export as… / Audio Overview.
          Only visible once the report is FINISHED and present. */}
      {r.status === "FINISHED" && r.report && r.cid && (
        <div className="mx-auto w-full max-w-doc">
          <NeedMoreCard
            report={r.report}
            cid={r.cid}
            onFollowUp={(question) => r.followUp(question)}
            followUpBusy={r.followUpStatus === "follow_up"}
            followUps={r.followUps}
          />
        </div>
      )}

      {/* Footer: settings link (calm) */}
      {r.status === "FINISHED" && (
        <footer className="flex items-center justify-end pt-section">
          <Link
            to="/settings"
            className="flex items-center gap-hair font-ui text-[0.74rem] text-text-faint hover:text-text-muted"
          >
            <SettingsIcon className="size-3" aria-hidden />
            Settings
          </Link>
        </footer>
      )}
    </>
  );
}
