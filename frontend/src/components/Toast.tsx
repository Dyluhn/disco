/**
 * A tiny, dependency-free toast system. Transient, self-dismissing notices that
 * never block — used for honesty cues like "you just picked a PAID model, and it
 * now drives everything." Intentionally minimal: a context + a portal region at
 * the app root, auto-dismiss, click-to-dismiss. No new package.
 *
 * Design discipline: chroma carries meaning. `tone="cost"` (the paid-model notice)
 * uses the accent rail so a billing-relevant message reads differently from a
 * neutral one — without being alarmist.
 */

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { CircleDollarSign, Info, X } from "lucide-react";
import { cn } from "@/lib/cn";

export type ToastTone = "neutral" | "cost";

interface Toast {
  id: number;
  title: string;
  body?: string;
  tone: ToastTone;
}

interface ToastApi {
  /** Show a transient toast. Returns its id (so a caller could dismiss early). */
  show: (t: { title: string; body?: string; tone?: ToastTone; ttlMs?: number }) => number;
}

const ToastContext = createContext<ToastApi | null>(null);

/** Access the toast API. Safe no-op if no provider is mounted (e.g. unit tests
 *  that render a pill in isolation) — so callers never need to guard. */
export function useToast(): ToastApi {
  return useContext(ToastContext) ?? { show: () => -1 };
}

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const seq = useRef(0);
  const timers = useRef(new Map<number, ReturnType<typeof setTimeout>>());

  const dismiss = useCallback((id: number) => {
    setToasts((ts) => ts.filter((t) => t.id !== id));
    const h = timers.current.get(id);
    if (h) {
      clearTimeout(h);
      timers.current.delete(id);
    }
  }, []);

  const show = useCallback<ToastApi["show"]>(
    ({ title, body, tone = "neutral", ttlMs = 6000 }) => {
      const id = ++seq.current;
      setToasts((ts) => [...ts, { id, title, body, tone }]);
      timers.current.set(
        id,
        setTimeout(() => dismiss(id), ttlMs),
      );
      return id;
    },
    [dismiss],
  );

  useEffect(() => {
    const t = timers.current;
    return () => t.forEach(clearTimeout);
  }, []);

  return (
    <ToastContext.Provider value={{ show }}>
      {children}
      {typeof document !== "undefined" &&
        createPortal(
          <div
            className="pointer-events-none fixed inset-x-0 bottom-4 z-[60] flex flex-col items-center gap-inline px-body"
            role="region"
            aria-label="Notifications"
          >
            {toasts.map((t) => (
              <div
                key={t.id}
                role="status"
                className={cn(
                  "pointer-events-auto flex w-[min(28rem,92vw)] items-start gap-inline rounded-card border bg-surface-1/95 px-body py-inline shadow-lg backdrop-blur pmx-rise",
                  t.tone === "cost" ? "border-accent/40" : "border-hairline",
                )}
              >
                {t.tone === "cost" ? (
                  <CircleDollarSign className="mt-px size-4 shrink-0 text-accent" aria-hidden />
                ) : (
                  <Info className="mt-px size-4 shrink-0 text-text-muted" aria-hidden />
                )}
                <div className="min-w-0 flex-1">
                  <p className="font-ui text-[0.84rem] font-medium text-text">{t.title}</p>
                  {t.body && (
                    <p className="mt-px font-ui text-[0.78rem] leading-snug text-text-muted">
                      {t.body}
                    </p>
                  )}
                </div>
                <button
                  type="button"
                  onClick={() => dismiss(t.id)}
                  aria-label="Dismiss"
                  className="shrink-0 rounded-control p-px text-text-faint transition-colors hover:text-text"
                >
                  <X className="size-3.5" aria-hidden />
                </button>
              </div>
            ))}
          </div>,
          document.body,
        )}
    </ToastContext.Provider>
  );
}
