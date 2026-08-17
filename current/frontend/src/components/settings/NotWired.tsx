import { AlertTriangle } from "lucide-react";

/**
 * A LOUD, unmissable marker for a surface whose controls do not yet affect the
 * running system. We do not let a control look operable when it changes nothing —
 * if it isn't wired, we say so in red, plainly, and disable the dead controls.
 *
 * `detail` must state precisely what is missing and what (if anything) the control
 * actually does, so the gap is obvious and directs what to build next.
 */
export function NotWired({ detail }: { detail: string }) {
  return (
    <div
      role="note"
      className="flex items-start gap-inline rounded-control border border-unsupported/60 bg-unsupported/10 px-body py-inline"
    >
      <AlertTriangle
        className="mt-px size-4 shrink-0 text-unsupported"
        aria-hidden
      />
      <p className="font-ui text-[0.8rem] leading-snug text-unsupported">
        <span className="font-semibold">Unavailable.</span> {detail}
      </p>
    </div>
  );
}
