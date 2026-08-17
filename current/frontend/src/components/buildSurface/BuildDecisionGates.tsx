/**
 * The per-action confirmation gate + the alternatives/questions-v2/clarify
 * decision gates. Extracted verbatim from BuildSurface.tsx (PKG-12-FE-BUILD).
 */

import { ConfirmationPanel } from "@/components/build/ConfirmationPanel";
import { AlternativesGate } from "@/components/build/AlternativesGate";
import { QuestionsV2Panel } from "@/components/build/QuestionsV2Panel";
import { ClarifyPanel } from "@/components/build/ClarifyPanel";
import type { BuildController } from "./types";

export function BuildDecisionGates({ b }: { b: BuildController }) {
  return (
    <>
      {b.pendingAction && (
        <ConfirmationPanel action={b.pendingAction} onApprove={b.confirm} onReject={b.reject} />
      )}
      {b.awaitingDecision && b.pendingAlternatives && (
        <AlternativesGate alternatives={b.pendingAlternatives} onPick={b.pickAlternative} />
      )}
      {b.awaitingQuestion && b.pendingQuestionsV2 && (
        <QuestionsV2Panel
          question={b.pendingQuestionsV2.question}
          items={b.pendingQuestionsV2.items.map((it) => ({
            id: it.id,
            question: it.question,
            options: it.options,
            allow_free_text: it.allow_free_text,
          }))}
          onAnswer={b.answer}
        />
      )}
      {b.awaitingQuestion && !b.pendingQuestionsV2 && b.pendingClarify && (
        <ClarifyPanel
          question={b.pendingClarify.question}
          items={b.pendingClarify.items.map((it) => ({
            id: it.id,
            question: it.question,
            type: it.type,
            options: it.options,
          }))}
          onAnswer={b.answer}
        />
      )}
    </>
  );
}
