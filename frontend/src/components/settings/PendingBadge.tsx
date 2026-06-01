import { Clock } from "lucide-react";

/**
 * An honest marker for scaffolded surfaces (Prompt 4): the surface is built and
 * looks complete, but its subsystem isn't wired yet. We say so plainly rather
 * than letting a control pretend to do more than it does.
 */
export function PendingBadge({ children = "Wiring pending" }: { children?: string }) {
  return (
    <span className="inline-flex items-center gap-hair rounded-[0.25rem] border border-hairline px-1 py-px font-ui text-[0.62rem] uppercase tracking-wide text-text-faint">
      <Clock className="size-3" aria-hidden />
      {children}
    </span>
  );
}
