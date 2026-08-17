import { createContext, useContext } from "react";

export type ToastTone = "neutral" | "cost";

export interface ToastApi {
  /** Show a transient toast. Returns its id so a caller can dismiss early. */
  show: (toast: {
    title: string;
    body?: string;
    tone?: ToastTone;
    ttlMs?: number;
  }) => number;
}

export const ToastContext = createContext<ToastApi | null>(null);

/** Safe no-op when no provider is mounted, including isolated unit tests. */
export function useToast(): ToastApi {
  return useContext(ToastContext) ?? { show: () => -1 };
}
