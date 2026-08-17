/**
 * Auto-scroll the Build activity feed to the bottom ONLY on a "you need to look
 * now" moment — never on every event (that would yank the user off whatever
 * they're reading). Two triggers: (1) the agent needs input (a gate just
 * opened), or (2) a capstone happened (the run finished/stuck/errored, or a
 * deliverable landed). We detect the TRANSITION into those, so re-renders
 * mid-state don't re-scroll. Separately, a continuous "follow the stream"
 * auto-scroll keeps the latest line in view while the user is already near the
 * bottom (W-40/W-41/F15). Extracted verbatim from BuildSurface.tsx.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type { ConversationStatus } from "@/types/agent";

function preferredScrollBehavior(): ScrollBehavior {
  if (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  ) {
    return "auto";
  }
  return "smooth";
}

function scrollElementTo(el: HTMLElement, top: number, behavior: ScrollBehavior) {
  if (typeof el.scrollTo === "function") el.scrollTo({ top, behavior });
  else el.scrollTop = top;
}

const NEEDS_INPUT_STATUSES = new Set<ConversationStatus>([
  "WAITING_FOR_CONFIRMATION",
  "AWAITING_USER_DECISION",
  "AWAITING_USER_QUESTION",
  "AWAITING_PLAN_APPROVAL",
]);
const CAPSTONE_STATUSES = new Set<ConversationStatus>(["FINISHED", "STUCK", "ERROR"]);

export function useFeedAutoScroll(
  status: ConversationStatus,
  deliverableId: string | null,
  activityLength: number,
  streamingContent: string | undefined,
) {
  const feedScrollRef = useRef<HTMLDivElement>(null);
  const prevStatusRef = useRef(status);
  const prevDeliverableRef = useRef<string | null>(null);
  // W-41: a docked "scroll to latest" chevron — shown only when the operator
  // has scrolled up far enough (>200px from the bottom) to have lost the live
  // tail; hidden once they're back at the bottom. The feed only auto-follows when
  // already near the bottom (below), so without this affordance a user reading
  // history has no quick way back to the live edge.
  const [showScrollDown, setShowScrollDown] = useState(false);

  const onFeedScroll = useCallback(() => {
    const el = feedScrollRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    setShowScrollDown(distanceFromBottom > 200);
  }, []);

  const scrollFeedToBottom = useCallback(() => {
    const el = feedScrollRef.current;
    if (!el) return;
    scrollElementTo(el, el.scrollHeight, preferredScrollBehavior());
    setShowScrollDown(false);
  }, []);

  useEffect(() => {
    const el = feedScrollRef.current;
    const prevStatus = prevStatusRef.current;
    const prevDeliverable = prevDeliverableRef.current;
    prevStatusRef.current = status;
    prevDeliverableRef.current = deliverableId;
    if (!el) return;

    const needsInput = NEEDS_INPUT_STATUSES.has(status);
    const capstone = CAPSTONE_STATUSES.has(status);
    const statusTransitioned = status !== prevStatus;
    const newDeliverable = deliverableId !== null && deliverableId !== prevDeliverable;

    // W-40: the plan-approval gate renders the PlanPanel FIRST, at the TOP of the
    // feed — so scrolling to the bottom (as every other gate does) pushes the
    // approval prompt off-screen, and after the first iteration only the top-left
    // spinner hints that input is needed. Special-case it to pull the operator to
    // the TOP. Confirm/decision/question gates + capstones still scroll to bottom.
    if (statusTransitioned && status === "AWAITING_PLAN_APPROVAL") {
      scrollElementTo(el, 0, preferredScrollBehavior());
    } else if ((statusTransitioned && (needsInput || capstone)) || newDeliverable) {
      scrollElementTo(el, el.scrollHeight, preferredScrollBehavior());
    }
  }, [status, deliverableId]);

  // Continuous "follow the stream" auto-scroll: as the feed grows during a run,
  // keep the latest line in view — but ONLY when the user is already near the
  // bottom (within 120px). If they've scrolled up to read history, we don't yank
  // them back down. Keyed on the activity length so it fires per new line.
  useEffect(() => {
    const el = feedScrollRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (distanceFromBottom < 120) {
      el.scrollTop = el.scrollHeight;
      setShowScrollDown(false);
    } else {
      // W-41: content grew while the user is scrolled up — re-evaluate so the
      // chevron appears as the tail moves further out of view.
      setShowScrollDown(distanceFromBottom > 200);
    }
  }, [activityLength, streamingContent]);

  return { feedScrollRef, onFeedScroll, showScrollDown, scrollFeedToBottom };
}
