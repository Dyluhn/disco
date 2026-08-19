/**
 * The pinned-under-the-feed composer region: uploads, the steer composer, the
 * re-plan composer, and the schedule section. Extracted verbatim from
 * BuildSurface.tsx (PKG-12-FE-BUILD).
 */

import type { ReactNode } from "react";
import { Check } from "lucide-react";
// NOTE: this is `components/build/BuildSurface.tsx` (the OTHER file) — not the
// file this directory decomposes. It is not ours to edit; only its
// `UploadComposer` export is consumed here, unchanged from the original.
import { UploadComposer } from "@/components/build/BuildSurface";
import { SteerInput } from "@/components/build/SteerInput";
import { BuildModelPicker } from "@/components/build/BuildModelPicker";
import { QueryInput } from "@/components/QueryInput";
import { ScheduleSection } from "@/components/settings/ScheduleSection";
import type { BuildController, FramingCopy } from "./types";
import type { BuildFraming } from "@/components/BuildSurface";

export function BuildComposerSection({
  b,
  copy,
  steerable,
  settled,
  terminalIncomplete,
  steerWithMention,
  requestPlanWithMention,
  elementMentionChip,
  framing,
}: {
  b: BuildController;
  copy: FramingCopy;
  steerable: boolean;
  settled: boolean;
  terminalIncomplete: boolean;
  steerWithMention: (text: string) => void;
  requestPlanWithMention: (text: string) => void;
  elementMentionChip: ReactNode;
  framing: BuildFraming;
}) {
  return (
    <>
      {/* BP-11: uploads are legal whenever the conversation exists — INCLUDING
          a fresh IDLE one (upload-then-build is the order's primary flow), so
          this renders outside the steerable gate. ERROR is the one exclusion:
          the backend 409s uploads into a dead sandbox. */}
      {b.cid && b.status !== "ERROR" && <UploadComposer cid={b.cid} />}
      {steerable && (
        <div className="flex flex-col gap-hair">
          {terminalIncomplete && (
            <p className="font-ui text-[0.74rem] text-warn">
              The run stopped before the plan was complete. Send a message to
              steer the agent back in, or use "Plan a change…" below to
              re-enter plan mode.
            </p>
          )}
          <SteerInput
            onSteer={steerWithMention}
            disabled={b.status === "WAITING_FOR_CONFIRMATION"}
            attachment={elementMentionChip}
          />
        </div>
      )}
      {settled && (
        <div className="flex flex-col gap-hair">
          <BuildModelPicker value={b.modelId} onChange={b.setModelId} surface={framing} />
          {/* F-3 follow-up: the EXPLICIT "accept as-is" path. Sends the authoritative
              accept_finished frame — the UI states stop-intent instead of the server
              guessing it from typed prose ("ship it" in the composer below remains
              the free-text fallback). FINISHED only: there is nothing to accept on
              an IDLE/STUCK settled state. */}
          {b.status === "FINISHED" && (
            <button
              type="button"
              onClick={b.acceptFinished}
              aria-label="Mark the build done — accept it as-is"
              title="Accept the finished build as-is (no re-plan)"
              data-disco-control="build.mark-done"
              className="flex max-lg:min-h-11 items-center gap-hair self-start rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:border-accent hover:text-text"
            >
              <Check className="size-3.5" aria-hidden />
              Mark done
            </button>
          )}
          {/* re-enter plan mode: a focused, diff-style change is planned + re-approved */}
          <QueryInput
            onSubmit={requestPlanWithMention}
            placeholder={copy.replanPlaceholder}
            controlId="replan-send"
            footer={!steerable ? elementMentionChip : undefined}
          />
        </div>
      )}
      {/* RP-08: schedule this conversation to re-run on a cron cadence. Only
          meaningful once it's settled and has a persisted conversation id. */}
      {settled && b.cid && (
        <div className="border-t border-hairline pt-inline">
          <ScheduleSection conversationId={b.cid} />
        </div>
      )}
    </>
  );
}
