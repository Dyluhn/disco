/**
 * SelectionOverlay — host-side chrome layered over a preview iframe.
 *
 * Renders:
 *  - An "Inspect" / arm toggle button in the top-right corner.
 *  - A transparent intercept layer (pointer-events: none) when armed, so
 *    the cursor changes to crosshair over the iframe area.
 *  - A positioned rect box + label when a selection has been received.
 *  - A "↑ parent" (walk-up) button and dismiss control on the label bar.
 *
 * **Placement:** must be rendered inside a `position: relative` container
 * that exactly covers the iframe it decorates.  The iframe's flex sibling
 * should be wrapped like:
 *
 *   <div className="relative min-h-0 flex-1">
 *     <iframe ref={iframeRef} className="h-full w-full ..." />
 *     <SelectionOverlay ... />
 *   </div>
 *
 * **Untrusted content:** when `untrusted` is true the arm toggle is replaced
 * with a "Inspect disabled (untrusted)" label — no dead control is shown
 * (spec: no false affordances).
 *
 * The component is PURE — all state lives in the parent via `useElementSelect`.
 */

import { useEffect, useRef, useState } from "react";
import { ChevronUp, MessageSquare, MousePointer2, X } from "lucide-react";
import type { SelectionEnvelope } from "@/lib/selectionBridge";

/** W-27: a self-dismissing first-run hint explaining the Inspect feature.
 * Shows once (localStorage flag), counts down visibly from 10s, auto-hides at 0,
 * and can be dismissed early with the X. Self-contained — no parent wiring. */
const INSPECT_TIP_SEEN_KEY = "disco-inspect-tip-seen";

function InspectTip() {
  // Seed from localStorage on first render so the tip never flashes for a
  // returning user. SSR-safe guard (no window during prerender).
  const [show, setShow] = useState(() => {
    if (typeof window === "undefined") return false;
    try {
      return window.localStorage.getItem(INSPECT_TIP_SEEN_KEY) == null;
    } catch {
      return false;
    }
  });
  const [seconds, setSeconds] = useState(10);
  const dismissed = useRef(false);

  function dismiss() {
    if (dismissed.current) return;
    dismissed.current = true;
    setShow(false);
    try {
      window.localStorage.setItem(INSPECT_TIP_SEEN_KEY, "1");
    } catch {
      /* private mode / disabled storage — fine, just won't persist */
    }
  }

  useEffect(() => {
    if (!show) return;
    const id = setInterval(() => {
      setSeconds((s) => {
        if (s <= 1) {
          clearInterval(id);
          dismiss();
          return 0;
        }
        return s - 1;
      });
    }, 1000);
    return () => clearInterval(id);
  }, [show]);

  if (!show) return null;

  return (
    <div
      className="absolute right-2 top-10 z-[103] flex max-w-[220px] items-start gap-1.5 rounded border border-hairline bg-surface-1/95 px-2 py-1.5 shadow-md backdrop-blur-sm select-none"
      role="status"
    >
      <MousePointer2 className="mt-0.5 size-3 shrink-0 text-accent" aria-hidden />
      <div className="min-w-0 flex-1">
        <p className="font-ui text-[0.7rem] leading-snug text-text">
          <span className="font-medium">Inspect:</span> highlight an item and discuss or
          iterate on it with the agent.
        </p>
        <p className="mt-0.5 font-ui text-[0.62rem] text-text-faint">Hiding in {seconds}s</p>
      </div>
      <button
        type="button"
        onClick={dismiss}
        aria-label="Dismiss inspect tip"
        className="shrink-0 rounded p-0.5 text-text-faint hover:text-text"
      >
        <X className="size-3" aria-hidden />
      </button>
    </div>
  );
}

interface Props {
  /** Whether the selection mode is currently active. */
  armed: boolean;
  /** True for shared/imported runs — disables the arm toggle. */
  untrusted: boolean;
  /** Most recently received selection, or null. */
  selection: SelectionEnvelope | null;
  /** Called when the user clicks the "Inspect" button. */
  onArm: () => void;
  /** Called when the user clicks "Stop inspecting" or the dismiss X. */
  onDisarm: () => void;
  /** Called when the user clicks the "↑ parent" button. */
  onWalkUp: () => void;
  /** W-26 — when provided, a "Discuss with agent" button appears on the selection
   * label bar for ANY selection kind (source / deck / non-source). The parent only
   * passes this when steering is actually available (`steerable`), so the action is
   * never a false affordance during disabled/non-steerable states. */
  onDiscuss?: (sel: SelectionEnvelope) => void;
}

export function SelectionOverlay({
  armed,
  untrusted,
  selection,
  onArm,
  onDisarm,
  onWalkUp,
  onDiscuss,
}: Props) {
  const rect = selection?.rect ?? null;
  const label = selection?.human_label ?? null;

  // Clamp the label bar so it doesn't escape the top of the overlay area.
  const labelLeft = rect ? Math.max(0, rect.x) : 0;
  const labelTop = rect ? rect.y + rect.height + 4 : 0;

  return (
    <>
      {/* ── Arm / untrusted notice ──────────────────────────────────────── */}
      <div className="absolute right-2 top-2 z-[102] flex items-center gap-1 select-none">
        {untrusted ? (
          <span
            className="rounded border border-hairline bg-surface-1/90 px-2 py-0.5 font-ui text-[0.67rem] text-text-faint backdrop-blur-sm"
            role="status"
          >
            Inspect disabled (untrusted)
          </span>
        ) : armed ? (
          <button
            type="button"
            onClick={onDisarm}
            className="flex items-center gap-1 rounded border border-accent bg-surface-1/90 px-2 py-0.5 font-ui text-[0.7rem] text-accent shadow-sm backdrop-blur-sm hover:bg-surface-2"
          >
            <MousePointer2 className="size-3" aria-hidden />
            Stop inspecting
          </button>
        ) : (
          <button
            type="button"
            onClick={onArm}
            className="flex items-center gap-1 rounded border border-hairline bg-surface-1/90 px-2 py-0.5 font-ui text-[0.7rem] text-text-muted shadow-sm backdrop-blur-sm hover:border-accent hover:text-text"
          >
            <MousePointer2 className="size-3" aria-hidden />
            Inspect
          </button>
        )}
      </div>

      {/* ── W-27: first-run inspect explainer (only when inspect is usable) ─── */}
      {!untrusted && <InspectTip />}

      {/* ── Armed: crosshair layer (pointer-events:none so iframe gets events) */}
      {armed && !untrusted && (
        <div
          className="absolute inset-0 z-[99] cursor-crosshair"
          style={{ pointerEvents: "none" }}
          aria-hidden
        />
      )}

      {/* ── Selection rect highlight ─────────────────────────────────────── */}
      {rect !== null && (
        <div
          className="absolute z-[100] box-border border-2 border-accent bg-accent/10"
          style={{
            left: rect.x,
            top: rect.y,
            width: rect.width,
            height: rect.height,
            pointerEvents: "none",
          }}
          aria-hidden
        />
      )}

      {/* ── Label bar + walk-up + dismiss ───────────────────────────────── */}
      {rect !== null && label !== null && (
        <div
          className="absolute z-[101] flex items-center gap-0.5 rounded bg-accent px-1.5 py-0.5 shadow"
          style={{
            left: labelLeft,
            top: labelTop,
            pointerEvents: "auto",
          }}
        >
          <span className="max-w-[180px] truncate font-mono text-[0.62rem] leading-none text-white">
            {label}
          </span>
          {/* W-26 — "Discuss with agent": delivers the selection context to the
              agent (send_message). Shown ONLY when `onDiscuss` is wired, which the
              parent does only when steering is available — so no false affordance
              appears during disabled / non-steerable states. Works for ALL
              selection kinds (source / deck / non-source), not just source edits. */}
          {onDiscuss && selection && (
            <button
              type="button"
              onClick={() => onDiscuss(selection)}
              title="Discuss this element with the agent"
              data-disco-control="build.selection-discuss"
              className="flex shrink-0 items-center gap-px rounded px-0.5 py-px font-ui text-[0.6rem] leading-none text-white/85 hover:text-white"
            >
              <MessageSquare className="size-3" aria-hidden />
              Discuss
            </button>
          )}
          <button
            type="button"
            onClick={onWalkUp}
            title="Select parent element"
            className="flex shrink-0 items-center rounded px-0.5 py-px text-white/70 hover:text-white"
          >
            <ChevronUp className="size-3" aria-hidden />
          </button>
          <button
            type="button"
            onClick={onDisarm}
            title="Dismiss selection"
            className="flex shrink-0 items-center rounded px-0.5 py-px text-white/70 hover:text-white"
          >
            <X className="size-3" aria-hidden />
          </button>
        </div>
      )}
    </>
  );
}
