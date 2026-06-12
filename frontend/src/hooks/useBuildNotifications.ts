/**
 * Attention when you've tabbed away from a build. Watches the conversation status
 * and, on a transition the user would want to know about WHILE the tab is hidden,
 * badges `document.title` and (best-effort) fires an OS Notification:
 *
 *   - the run reached a capstone (FINISHED / ERROR / STUCK), or
 *   - the agent needs you (a confirmation / plan / decision / question gate opened).
 *
 * The title badge needs no permission and always works; the OS Notification is a
 * bonus when the user has granted it. The title restores when the tab refocuses.
 *
 * Why gate on document.hidden: if the tab is visible the user already sees the UI —
 * a badge/notification would be noise. The point is reaching someone who looked away.
 */

import { useEffect, useRef } from "react";
import type { ConversationStatus } from "@/types/agent";

const CAPSTONE: ConversationStatus[] = ["FINISHED", "ERROR", "STUCK"];
const NEEDS_YOU: ConversationStatus[] = [
  "WAITING_FOR_CONFIRMATION",
  "AWAITING_PLAN_APPROVAL",
  "AWAITING_USER_DECISION",
  "AWAITING_USER_QUESTION",
];

function badgeFor(status: ConversationStatus): string | null {
  if (status === "FINISHED") return "✓ Build finished";
  if (status === "ERROR") return "⚠ Build failed";
  if (status === "STUCK") return "⚠ Build stuck";
  if (NEEDS_YOU.includes(status)) return "● Needs your input";
  return null;
}

export function useBuildNotifications(
  status: ConversationStatus,
  taskLabel?: string | null,
): void {
  const prev = useRef<ConversationStatus>(status);
  // The pristine title to restore to (captured once, before we ever badge it).
  const baseTitle = useRef<string>(typeof document !== "undefined" ? document.title : "");

  // Lazily ask for Notification permission once a run is actually under way — close
  // enough to the user's submit gesture, and only if they haven't decided yet.
  useEffect(() => {
    if (status !== "RUNNING") return;
    if (typeof window === "undefined" || !("Notification" in window)) return;
    if (Notification.permission === "default") {
      void Notification.requestPermission().catch(() => {});
    }
  }, [status]);

  // Badge + notify on the meaningful transition, but only while tabbed away.
  useEffect(() => {
    const was = prev.current;
    prev.current = status;
    if (was === status) return;

    const isCapstone = CAPSTONE.includes(status) && !CAPSTONE.includes(was);
    const needsYou = NEEDS_YOU.includes(status) && !NEEDS_YOU.includes(was);
    if (!isCapstone && !needsYou) return;
    if (typeof document === "undefined" || !document.hidden) return; // visible → no noise

    const badge = badgeFor(status);
    if (!badge) return;
    document.title = `${badge} · ${baseTitle.current}`;

    try {
      if (typeof window !== "undefined" && "Notification" in window && Notification.permission === "granted") {
        new Notification(badge, { body: taskLabel || "Disco build" });
      }
    } catch {
      /* notifications are a bonus — never let them throw into the render path */
    }
  }, [status, taskLabel]);

  // Restore the pristine title the moment the user comes back to the tab.
  useEffect(() => {
    if (typeof document === "undefined") return;
    function onVisible() {
      if (!document.hidden) document.title = baseTitle.current;
    }
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, []);
}
