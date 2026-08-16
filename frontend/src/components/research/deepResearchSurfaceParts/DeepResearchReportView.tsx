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
import { DeepReportView } from "../DeepReportView";
import { asGroundedAnswer } from "../groundedAnswer";
import { TieredSourcePanel } from "../TieredSourcePanel";
import { NeedMoreCard } from "../NeedMoreCard";
import { FollowUpStatus } from "../FollowUpStatus";

interface Props {
  r: ReturnType<typeof useDeepResearch>;
}

export function DeepResearchReportView({ r }: Props) {
  return (
    <>
      {/* The report — assembles section-by-section while RUNNING, settled
          after FINISHED. Always rendered once we have a plan so the
          reader sees the document forming. */}
      {!r.awaitingPlan && r.plan && (
        <DeepReportView
          query={r.query ?? ""}
          summary={r.report?.summary ?? null}
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
              <div
                key={i}
                className="rounded-card border border-hairline bg-surface-1 px-body py-body"
              >
                <p className="mb-inline font-ui text-[0.72rem] uppercase tracking-wide text-text-faint">
                  Answer
                </p>
                {/* Render through <Markdown> so the model's markdown (bold,
                    lists, code, etc.) formats properly. Pass the report's
                    passage corpus so [[id]] citation markers resolve to chips
                    instead of staying as raw brackets. */}
                <div className="prose-reading">
                  <Markdown answer={asGroundedAnswer(r.report, r.query ?? "")}>
                    {m.message.content}
                  </Markdown>
                </div>
              </div>
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
