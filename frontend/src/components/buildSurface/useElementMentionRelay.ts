/**
 * P8 element-mention relay: holds the pending click-to-edit mention payload and
 * wraps `b.steer`/`b.requestPlan` so the mention is prepended to the next
 * steer/re-plan message and then cleared. Extracted verbatim from
 * BuildSurface.tsx — same dependency arrays (keyed on the whole controller,
 * matching the original's memoization) so behavior is unchanged.
 */

import { useCallback, useState } from "react";
import { prependElementMention } from "@/lib/elementMention";
import type { ElementMentionPayload } from "@/lib/elementMention";
import type { BuildController } from "./types";

export function useElementMentionRelay(b: BuildController) {
  const [elementMention, setElementMention] = useState<ElementMentionPayload | null>(null);

  const consumeElementMention = useCallback(
    (text: string) => {
      const content = prependElementMention(text, elementMention);
      if (elementMention) setElementMention(null);
      return content;
    },
    [elementMention],
  );

  const steerWithMention = useCallback(
    (text: string) => b.steer(consumeElementMention(text)),
    [b, consumeElementMention],
  );

  const requestPlanWithMention = useCallback(
    (text: string) => b.requestPlan(consumeElementMention(text)),
    [b, consumeElementMention],
  );

  return { elementMention, setElementMention, steerWithMention, requestPlanWithMention };
}
