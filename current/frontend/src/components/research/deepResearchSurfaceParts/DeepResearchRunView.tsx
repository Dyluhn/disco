/**
 * DeepResearchRunView — the in-flight run machinery: the error state, the
 * start-up state, the hold decision panel, the progress strip, and the
 * bounded-by notice.
 *
 * v2 (gateless deep research): the plan-edit gate is GONE from this surface.
 * Submitting starts the research, so there is nothing to approve or revise —
 * the strip mounts as soon as the run is live and the model's brief is the
 * first thing in it. See DeepResearchSurface.tsx's header for the surface map.
 *
 * L24: two things here stop pretending. The start-up state was a spinner with
 * no escape — it looked identical after 3 seconds and after 3 minutes; it now
 * ages off the moment this client opened the stream and says so. And when the
 * loop holds on a cooling search pool, that is surfaced as the user's choice
 * (keep waiting / stop with what it has) rather than as silence or a failure.
 *
 * L29 adds the third: the stretch between Stop and the run's own ending. It
 * used to be invisible — a terminal IDLE erased the run 29 ms after the click
 * while the engine worked on for minutes — and is now the stopping wall.
 */
import { ErrorState } from "@/components/states";
import { useNowMs } from "@/hooks/useNowMs";
import type { useDeepResearch } from "@/hooks/useDeepResearch";
import { ActivitySignal } from "../ActivitySignal";
import { signalReading, writerLegLeads } from "@/lib/deepResearchHeartbeat";
import { killedCopy } from "@/lib/deepResearchStopping";
import { useRef, useState } from "react";
import { DeepBoundedNotice } from "../DeepBoundedNotice";
import { DeepResearchCheckpoint } from "../DeepResearchCheckpoint";
import { DeepResearchHoldPanel } from "../DeepResearchHoldPanel";
import { DeepResearchStoppingWall } from "../DeepResearchStoppingWall";
import { DeepProgressStrip } from "../DeepProgressStrip";
import { DeepSteerInput } from "../DeepSteerInput";

interface Props {
  r: ReturnType<typeof useDeepResearch>;
}

/** The gap between submit and the engine's first event. Without a signal this
 *  area is blank, which reads as a hang — but a spinner claims work nobody
 *  reported. So it says exactly what is true: nothing has arrived since the
 *  stream opened, and for how long. */
function StartingState({ lastEventAt }: { lastEventAt: number | null }) {
  const openedAt = useRef(Date.now());
  const nowMs = useNowMs(true);
  const signal = signalReading(lastEventAt, nowMs, openedAt.current);
  return (
    <div
      data-dr-starting=""
      className="flex flex-wrap items-center gap-inline font-ui text-[0.86rem] text-text-muted"
    >
      <ActivitySignal signal={signal} />
      <span>
        {signal.level === "live"
          ? "Starting the research — waiting for the engine's first update."
          : "Nothing has arrived from the engine since this run opened. Stop ends it and keeps whatever it has."}
      </span>
    </div>
  );
}

/** Stop has been asked for and the run has not answered yet. It clears itself
 *  the moment the run writes ANY status — the PAUSED checkpoint it stops at, or
 *  the RUNNING a later resume opens with — so it is never sticky. */
function isStopping(r: ReturnType<typeof useDeepResearch>): boolean {
  return r.status === "RUNNING" && r.activity.stopRequested !== null;
}

/** The strip is worth showing the moment there is ANY run signal — the brief,
 *  a trace row, a checkpoint or a finished report. Before that the run has
 *  produced nothing, and an empty strip would be chrome around a void. */
function hasRunSignal(r: ReturnType<typeof useDeepResearch>): boolean {
  return Boolean(r.brief) || r.trace.length > 0 || Boolean(r.report) || Boolean(r.checkpoint);
}

/**
 * The hold, and whether the user has already answered it.
 *
 * The acknowledgement is keyed on the hold EPISODE so the re-synced `hold`
 * events the loop emits cannot re-ask a question the user answered, while a
 * genuinely new hold (after a resume) does get the panel again.
 */
function useHoldDecision(r: ReturnType<typeof useDeepResearch>) {
  const hold = r.activity.hold;
  const [waitedThrough, setWaitedThrough] = useState<string | null>(null);
  const nowMs = useNowMs(hold !== null);
  const acknowledged = hold !== null && waitedThrough === hold.episodeId;
  return {
    /** The hold to put a decision panel up for, or null. */
    panel: hold !== null && !acknowledged && r.status === "RUNNING" ? hold : null,
    /** The hold to render as the collapsed heartbeat line, or null. */
    line: acknowledged ? hold : null,
    nowMs,
    keepWaiting: () => setWaitedThrough(hold?.episodeId ?? null),
  };
}

/** The honest notice — when the engine bounded out, or when a review finding
 *  was still open as the report was published. It renders nothing when there
 *  is nothing to disclose. */
function BoundedSection({ r }: Props) {
  const report = r.report;
  if (!report) return null;
  return (
    <DeepBoundedNotice
      report={report}
      onTryExhaustive={
        report.depth_tier === "exhaustive"
          ? undefined
          : () => {
              // fix-c #5: use the dedicated callback — set-then-submit closed
              // over the OLD depthTier and re-ran at the same bounded tier.
              // runExhaustive takes the tier directly.
              if (r.query) r.runExhaustive(r.query);
            }
      }
    />
  );
}

/** Stop was pressed and the run has not written its ending yet.
 *
 *  The decision and its ticking clock live here rather than in the run view so
 *  the view keeps ONE branch for the whole stopping state — and so the clock
 *  only ticks while there is something aging to show. */
function StoppingSection({ r }: Props) {
  const nowMs = useNowMs(isStopping(r));
  if (!isStopping(r)) return null;
  return (
    <DeepResearchStoppingWall
      activity={r.activity}
      nowMs={nowMs}
      sourcesDiscovered={r.stats.sourcesDiscovered}
      onKill={r.kill}
    />
  );
}

/** A killed run's terminal. Not an error treatment: nothing failed, a person
 *  decided — the same quiet aside the hold panel and the checkpoint notice use.
 *  Owns its own condition for the same reason the bounded notice does: the run
 *  view asks for the section, not for the state machine behind it. */
function KilledNotice({ r }: Props) {
  if (r.status !== "IDLE" || r.statusDetail !== "killed" || r.report) return null;
  const copy = killedCopy(r.stats.sourcesDiscovered);
  return (
    <section
      aria-label={copy.title}
      data-dr-killed=""
      className="rounded-card border border-hairline-strong bg-surface-1 px-body py-body"
    >
      <h2 className="font-ui text-[0.9rem] font-medium text-text">{copy.title}</h2>
      <p className="mt-hair font-ui text-[0.82rem] leading-snug text-text-muted">
        {copy.why} {copy.stateNow}
      </p>
      <p className="mt-inline font-ui text-[0.82rem] leading-snug text-text">{copy.next}</p>
    </section>
  );
}

export function DeepResearchRunView({ r }: Props) {
  const signal = hasRunSignal(r);
  const decision = useHoldDecision(r);
  const stopping = isStopping(r);
  return (
    <>
      {/* Error state */}
      {r.status === "ERROR" && (
        <ErrorState
          message={r.error ?? "The research run failed."}
          onRetry={r.retry}
          failure={r.failure}
        />
      )}

      {/* Start-up state — no run signal yet. */}
      {!signal && r.status === "RUNNING" && (
        <StartingState lastEventAt={r.activity.lastEventAt} />
      )}

      {/* Stop was pressed and the run is finishing the step it is in. This
          outranks the hold panel below: once Stop is asked for, "keep waiting
          or stop" is no longer the live question. */}
      <StoppingSection r={r} />

      {/* Where the stopping wall just stood: the state the user created by
          pressing Stop. It used to mount below the whole trace, which on a long
          run is off-screen — so the run appeared to end in silence exactly
          where the wall had been answering. `deriveResearchCheckpoint` already
          returns null once a newer status is not PAUSED, so this cannot linger
          into a resumed run. */}
      {r.checkpoint && !r.report && (
        <DeepResearchCheckpoint checkpoint={r.checkpoint} />
      )}

      {/* The loop is holding on the search pool. Not a failure — a choice.
          Continue waiting sends NOTHING; Stop is the existing cancel. */}
      {decision.panel && !stopping && (
        <DeepResearchHoldPanel
          hold={decision.panel}
          nowMs={decision.nowMs}
          onContinue={decision.keepWaiting}
          onStop={r.stop}
        />
      )}

      {/* During the run + after: the progress strip (collapses on finish) */}
      {signal && (
        <DeepProgressStrip
          brief={r.brief}
          trace={r.trace}
          stats={r.stats}
          status={r.status}
          activity={r.activity}
          followUpStatus={r.followUpStatus}
          // The collapsed hold line carries its own Stop link. Once Stop has
          // been asked for, that link is the same false affordance the top
          // bar's button would be, so the line stands down with it.
          collapsedHold={stopping ? null : decision.line}
          onStopHold={r.stop}
          connectionState={r.connectionState}
        />
      )}

      {/* Mid-run steering. The transport was live from D3 but had no control
          until v2 — without this the user can watch the research but not
          redirect it. Live only while the run is, and only when a report has
          not landed (after that, questions are follow-ups). */}
      {/* …and not while stopping: the run is winding down, so a steer that
          would only reach a turn boundary that will never come is a false
          affordance. */}
      {r.status === "RUNNING" && !r.report && !stopping && (
        writerLegLeads(r.activity) ? (
          <p role="status" className="font-ui text-sm text-text-muted">
            Research is complete. You can send a follow-up when the report is ready.
          </p>
        ) : <DeepSteerInput onSteer={r.steer} />
      )}

      <BoundedSection r={r} />

      {/* The OTHER user-initiated ending. A killed run leaves no checkpoint, so
          the checkpoint panel above has nothing to show and the run would
          otherwise end in silence — the same silence Stop used to end in. */}
      <KilledNotice r={r} />
    </>
  );
}
