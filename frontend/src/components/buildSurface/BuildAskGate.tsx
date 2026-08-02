/**
 * The free-form Ask-gate: the driver-outage honesty banner (interactive
 * flavor) + the AskPanel itself. Extracted verbatim from BuildSurface.tsx
 * (PKG-12-FE-BUILD).
 */

import { DriverOutageBanner } from "@/components/build/DriverOutageBanner";
import { AskPanel } from "@/components/build/AskPanel";
import type { BuildController } from "./types";

export function BuildAskGate({
  b,
  finalMessage,
}: {
  b: BuildController;
  finalMessage: string | null;
}) {
  return (
    <>
      {/* Interactive flavor of the same honesty: the driver-outage landing
          parks at the ask gate, so the WHY (usage/rate limit vs outage)
          renders beside the question instead of only the generic blocked copy. */}
      {b.driverOutage && b.awaitingQuestion && <DriverOutageBanner outage={b.driverOutage} />}
      {b.awaitingQuestion && !b.pendingQuestionsV2 && !b.pendingClarify && (
        <AskPanel
          question={b.pendingQuestion?.message?.content ?? finalMessage ?? "The agent has a question."}
          onAnswer={b.answer}
        />
      )}
    </>
  );
}
