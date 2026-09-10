/**
 * Build surface split layout — chat pane (left) + inspector pane (right) with:
 *
 *   - draggable handle to resize the inspector pane (chat takes the remainder)
 *   - "collapse inspector" button: hides the right pane, chat takes the full width
 *   - clamped min/max so neither pane ever fully starves the other
 *   - persistent width + collapsed state via localStorage (survives reloads)
 *
 * Below `lg` this is NOT a shrunk side-by-side split (that squeezed the inspector
 * to a fixed 280px on phones — the bug this file used to have). Mobile gets its
 * own idiom: the chat pane (live activity feed + the composer) fills the phone's
 * full height, WITH THE SAME internal-scroll shape the desktop chat pane already
 * has — the feed scrolls, the composer stays pinned at the bottom, always
 * reachable without hunting for it. Files/Terminal/Preview/Cockpit (or the Agent
 * surface's Browser/Artifacts/Console) are occasional-use, so they're tucked
 * behind a small "Inspector" toggle and open as a full-screen sheet over the chat
 * — reachable in one tap, but never competing with the feed/composer for the
 * phone's limited vertical space by default. One `inspector` subtree is mounted
 * ONCE (never duplicated between a desktop copy and a mobile copy) — only its
 * position/visibility differs per breakpoint, via CSS.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { PanelRightClose, PanelRightOpen, GripVertical, X } from "lucide-react";
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
  // Mobile-only sheet visibility — deliberately NOT persisted (unlike `collapsed`
  // above): every run/visit starts on the chat pane, the moment-to-moment view.
  const [mobileInspectorOpen, setMobileInspectorOpen] = useState(false);

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
      className="relative flex h-full min-h-0 flex-col lg:flex-row"
    >
      <aside
        className={cn(
          "flex h-full min-h-0 flex-1 flex-col border-hairline lg:shrink-0 lg:overflow-hidden lg:border-r",
          "lg:min-w-[var(--chat-min-w)] lg:flex-1",
        )}
        style={{ "--chat-min-w": `${MIN_CHAT_PX}px` } as React.CSSProperties}
      >
        {/* Mobile-only entry point to the occasional stuff (files, terminal,
            preview, cockpit / browser, artifacts, console). A small utility
            strip, not a heavy chrome band — the feed + composer stay the
            hero. Gone entirely at lg (the inline split pane takes over). */}
        <div className="flex shrink-0 items-center justify-end px-body py-hair lg:hidden">
          <button
            type="button"
            onClick={() => setMobileInspectorOpen(true)}
            aria-label="Open inspector — files, terminal, preview"
            data-disco-control="build.mobile-inspector-open"
            className="flex min-h-11 items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:bg-surface-2 hover:text-text"
          >
            <PanelRightOpen className="size-3.5" aria-hidden />
            Inspector
          </button>
        </div>
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

      {/* ONE mount of `inspector` — below `lg` it's a full-screen sheet over the
          chat pane (positioned `absolute` within this relatively-positioned
          container, so it covers the surface but not the shell's nav/mode
          chrome above it); at `lg`+ it's the inline split pane. Never both a
          desktop copy AND a mobile copy — that would double-mount whatever
          live polling/tab-state `inspector` owns. */}
      <section
        className={cn(
          "absolute inset-0 z-30 flex-col bg-bg",
          mobileInspectorOpen ? "flex" : "hidden",
          "lg:static lg:inset-auto lg:z-auto lg:min-h-0 lg:w-[var(--ins-w)] lg:min-w-[var(--ins-min-w)] lg:border-t-0",
          collapsed ? "lg:hidden" : "lg:flex",
        )}
        style={inspectorVars}
      >
        <div className="flex shrink-0 items-center justify-between border-b border-hairline px-body py-hair">
          <span className="font-ui text-[0.72rem] font-medium uppercase tracking-wide text-text-faint">
            Inspector
          </span>
          {/* Mobile: close the sheet, back to chat. Desktop: collapse the
              inline pane (chat takes the full width; a re-open tab remains). */}
          <button
            type="button"
            onClick={() => setMobileInspectorOpen(false)}
            aria-label="Close inspector — back to chat"
            className="flex min-h-11 items-center gap-hair rounded-control px-hair py-px font-ui text-[0.74rem] text-text-faint transition-colors hover:bg-surface-2 hover:text-text lg:hidden"
          >
            <X className="size-4" aria-hidden />
            Close
          </button>
          <button
            type="button"
            onClick={() => setCollapsed(true)}
            aria-label="Collapse inspector pane — chat takes the full width"
            className="hidden items-center gap-hair rounded-control px-hair py-px font-ui text-[0.74rem] text-text-faint transition-colors hover:bg-surface-2 hover:text-text lg:flex"
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
