/**
 * Build surface split layout — chat pane (left) + inspector pane (right) with:
 *
 *   - draggable handle to resize the inspector pane (chat takes the remainder)
 *   - "collapse inspector" button: hides the right pane, chat takes the full width
 *   - clamped min/max so neither pane ever fully starves the other
 *   - persistent width + collapsed state via localStorage (survives reloads)
 *
 * Mobile/narrow viewports fall back to stacked vertical layout (no handle).
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { PanelRightClose, PanelRightOpen, GripVertical } from "lucide-react";
import { cn } from "@/lib/cn";

const STORAGE_KEY = "pmx.build.inspectorWidth";
const COLLAPSED_KEY = "pmx.build.inspectorCollapsed";
// Pixel clamps — the chat side has a stricter min (it carries most state),
// the inspector side can shrink further before collapsing.
const MIN_CHAT_PX = 320;
const MIN_INSPECTOR_PX = 280;
const DEFAULT_INSPECTOR_PCT = 0.58; // ~58% of viewport, leaving 42% for chat

function readPersistedFraction(): number {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULT_INSPECTOR_PCT;
    const n = parseFloat(raw);
    if (Number.isFinite(n) && n > 0.2 && n < 0.9) return n;
  } catch {
    /* fall through */
  }
  return DEFAULT_INSPECTOR_PCT;
}

function readPersistedCollapsed(): boolean {
  try {
    return window.localStorage.getItem(COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

export function ResizableSplit({
  chat,
  inspector,
}: {
  chat: React.ReactNode;
  inspector: React.ReactNode;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [inspectorFrac, setInspectorFrac] = useState(readPersistedFraction);
  const [collapsed, setCollapsed] = useState(readPersistedCollapsed);
  const [dragging, setDragging] = useState(false);

  // Persist state changes (debounced naturally — only on commit, not every move).
  useEffect(() => {
    try {
      window.localStorage.setItem(STORAGE_KEY, String(inspectorFrac));
    } catch { /* swallow */ }
  }, [inspectorFrac]);
  useEffect(() => {
    try {
      window.localStorage.setItem(COLLAPSED_KEY, collapsed ? "1" : "0");
    } catch { /* swallow */ }
  }, [collapsed]);

  // Pointer-based resize. Capturing on the window lets the drag continue
  // even if the cursor moves outside the handle.
  const onPointerMove = useCallback((e: PointerEvent) => {
    const c = containerRef.current;
    if (!c) return;
    const rect = c.getBoundingClientRect();
    const total = rect.width;
    const insWidth = rect.right - e.clientX;
    // Clamp by both pixel mins. When the window is too narrow to honor both
    // mins, the floor wins (Math.min of the two bounds) so the inspector never
    // exceeds its allotted space and starves the chat.
    const minIns = MIN_INSPECTOR_PX;
    const maxIns = Math.max(total - MIN_CHAT_PX, MIN_INSPECTOR_PX);
    const lo = Math.min(minIns, maxIns);
    const clamped = Math.min(Math.max(insWidth, lo), maxIns);
    setInspectorFrac(clamped / total);
  }, []);
  const onPointerUp = useCallback(() => {
    setDragging(false);
    window.removeEventListener("pointermove", onPointerMove);
    window.removeEventListener("pointerup", onPointerUp);
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
  }, [onPointerMove]);
  const onPointerDown = useCallback(
    (e: React.PointerEvent) => {
      if (collapsed) return;
      e.preventDefault();
      setDragging(true);
      window.addEventListener("pointermove", onPointerMove);
      window.addEventListener("pointerup", onPointerUp);
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
    },
    [collapsed, onPointerMove, onPointerUp],
  );

  // Safety net: if the component unmounts mid-drag, tear down the window
  // listeners + restore the cursor so they don't leak / fire on an unmounted tree.
  useEffect(() => {
    return () => {
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", onPointerUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
  }, [onPointerMove, onPointerUp]);

  // These widths are a DESKTOP-ONLY concept (the draggable split). Below `lg`
  // the layout falls back to a stacked column (see file header), so the pixel/
  // percent values must only take effect at the `lg:` breakpoint — otherwise a
  // desktop-sized MIN_INSPECTOR_PX (or a persisted narrow inspectorFrac) gets
  // applied as a real width on a 375–430px phone screen, leaving the stacked
  // inspector squeezed into a slice of the viewport with dead space beside it.
  // CSS custom properties carry the value across the `lg:` media query; Tailwind
  // only *reads* them once that query matches.
  const inspectorVars = collapsed
    ? ({ "--ins-w": "0px", "--ins-min-w": "0px" } as React.CSSProperties)
    : ({
        "--ins-w": `${inspectorFrac * 100}%`,
        "--ins-min-w": `${MIN_INSPECTOR_PX}px`,
      } as React.CSSProperties);

  return (
    <div
      ref={containerRef}
      className="relative flex min-h-0 flex-col lg:h-full lg:flex-row"
    >
      <aside
        className={cn(
          "flex min-h-0 flex-col border-hairline lg:shrink-0 lg:overflow-hidden lg:border-r",
          "lg:min-w-[var(--chat-min-w)] lg:flex-1",
        )}
        style={{ "--chat-min-w": `${MIN_CHAT_PX}px` } as React.CSSProperties}
      >
        {chat}
      </aside>

      {/* Resize handle — only visible on lg screens, only active when not collapsed */}
      {!collapsed && (
        <div
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize inspector pane"
          tabIndex={0}
          onPointerDown={onPointerDown}
          className={cn(
            "group relative hidden shrink-0 cursor-col-resize bg-hairline transition-colors lg:flex lg:w-px lg:items-center lg:justify-center hover:bg-accent",
            dragging && "bg-accent",
          )}
        >
          <span className="absolute inset-y-0 -left-2 -right-2 z-10" aria-hidden />
          <GripVertical
            className={cn(
              "size-4 opacity-0 transition-opacity group-hover:opacity-100",
              dragging && "opacity-100",
              "text-accent",
            )}
            aria-hidden
          />
        </div>
      )}

      <section
        className={cn(
          "flex min-h-[55vh] w-full flex-col border-t border-hairline lg:min-h-0 lg:w-[var(--ins-w)] lg:min-w-[var(--ins-min-w)] lg:border-t-0",
          collapsed && "lg:hidden",
        )}
        style={inspectorVars}
      >
        <div className="flex items-center justify-between border-b border-hairline px-body py-hair">
          <span className="font-ui text-[0.72rem] font-medium uppercase tracking-wide text-text-faint">
            Inspector
          </span>
          <button
            type="button"
            onClick={() => setCollapsed(true)}
            aria-label="Collapse inspector pane — chat takes the full width"
            className="flex items-center gap-hair rounded-control px-hair py-px font-ui text-[0.74rem] text-text-faint transition-colors hover:bg-surface-2 hover:text-text"
          >
            <PanelRightClose className="size-3.5" aria-hidden />
            Collapse
          </button>
        </div>
        <div className="flex min-h-0 flex-1 flex-col">{inspector}</div>
      </section>

      {/* Re-open affordance — a small tab on the right edge when collapsed */}
      {collapsed && (
        <button
          type="button"
          onClick={() => setCollapsed(false)}
          aria-label="Show inspector pane"
          className="absolute right-0 top-1/2 z-20 hidden -translate-y-1/2 items-center gap-hair rounded-l-control border border-r-0 border-hairline bg-surface-1 px-hair py-inline font-ui text-[0.74rem] text-text-muted shadow-sm transition-colors hover:bg-surface-2 hover:text-text lg:flex"
        >
          <PanelRightOpen className="size-3.5" aria-hidden />
          Inspector
        </button>
      )}
    </div>
  );
}
