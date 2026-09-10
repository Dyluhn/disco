import { useCallback, useEffect, useRef, useState } from "react";
import { useToast, type ToastApi } from "@/components/toastApi";
import type { ConversationStatus } from "@/types/agent";

const TERMINAL_STATUSES = new Set<ConversationStatus>(["FINISHED", "ERROR", "STUCK"]);
const armedConversationIds = new Set<string>();

function notificationTitle(title: string | null | undefined): string {
  const label = title?.trim() || "this report";
  return `Deep research complete — ${label}`;
}

async function requestNotificationPermission(): Promise<void> {
  if (typeof window === "undefined" || !("Notification" in window)) return;
  if (Notification.permission !== "default") return;
  try {
    await Notification.requestPermission();
  } catch {
    // Permission prompts are best effort; the in-app toast still fires on done.
  }
}

function showBrowserNotification(title: string): void {
  if (typeof window === "undefined" || !("Notification" in window)) return;
  if (Notification.permission !== "granted") return;
  try {
    new Notification(title);
  } catch {
    // Browser notifications are a bonus; rendering and toasts must keep working.
  }
}

export function useDeepResearchDoneNotification({
  cid,
  status,
  title,
  toast: toastOverride,
}: {
  cid: string | null;
  status: ConversationStatus;
  title: string | null | undefined;
  toast?: ToastApi;
}): { armed: boolean; toggle: () => void } {
  const contextToast = useToast();
  const toast = toastOverride ?? contextToast;
  const [armed, setArmed] = useState(() => Boolean(cid && armedConversationIds.has(cid)));
  const prev = useRef<{ cid: string | null; status: ConversationStatus }>({ cid, status });

  useEffect(() => {
    setArmed(Boolean(cid && armedConversationIds.has(cid)));
  }, [cid]);

  const toggle = useCallback(() => {
    if (!cid) return;
    setArmed((wasArmed) => {
      const next = !wasArmed;
      if (next) {
        armedConversationIds.add(cid);
        void requestNotificationPermission();
      } else {
        armedConversationIds.delete(cid);
      }
      return next;
    });
  }, [cid]);

  useEffect(() => {
    const was = prev.current;
    prev.current = { cid, status };
    if (!cid || was.cid !== cid || was.status === status) return;
    if (!TERMINAL_STATUSES.has(status) || TERMINAL_STATUSES.has(was.status)) return;
    if (!armedConversationIds.has(cid)) return;

    armedConversationIds.delete(cid);
    setArmed(false);
    const titleText = notificationTitle(title);
    toast.show({ title: titleText });
    showBrowserNotification(titleText);
  }, [cid, status, title, toast]);

  return { armed, toggle };
}

export function resetDeepResearchDoneNotificationStateForTests(): void {
  armedConversationIds.clear();
}
