import { AlertTriangle } from "lucide-react";

/**
 * A LOUD marker for a scaffolded surface: built and looking complete, but its
 * subsystem isn't wired, so its controls change nothing in the running system.
 * Red and explicit — we never let a control quietly pretend to do more than it
 * does (see also the NotWired banner with the specifics).
 */
export function PendingBadge({ children = "Not wired" }: { children?: string }) {
  return (
    <span className="inline-flex items-center gap-hair rounded-[0.25rem] border border-unsupported/60 px-1 py-px font-ui text-[0.62rem] font-semibold uppercase tracking-wide text-unsupported">
      <AlertTriangle className="size-3" aria-hidden />
      {children}
    </span>
  );
}
