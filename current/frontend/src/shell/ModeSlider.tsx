import { Bot, Search } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useLocation, useNavigate } from "react-router-dom";
import { listConversations } from "@/api/conversations";
import { CodeBlocksIcon } from "@/components/icons/CodeBlocksIcon";
import { cn } from "@/lib/cn";
import { isConversationKilled, isFreshMode } from "@/lib/sessionResume";
import { useMode, type Mode } from "./mode";

/** Statuses that mean "this session is still going" — switching modes should
 * drop you back INTO it, not strand you on a splash while it runs. */
const ACTIVE_STATUSES = new Set([
  "RUNNING",
  "PAUSED",
  "AWAITING_PLAN_APPROVAL",
  "AWAITING_USER_QUESTION",
  "AWAITING_USER_DECISION",
]);

/** Where a mode switch should land: the most recent ACTIVE session of that
 * mode (resume), else the splash composer for it (new session on first send).
 * Search has no per-conversation resume route — it always lands on the splash. */
async function resumeTargetFor(mode: Mode): Promise<string> {
  if (mode === "search") return "/";
  if (isFreshMode(mode)) return "/";
  try {
    const rows = await listConversations();
    const hit = rows.find(
      (c) =>
        c.surface === mode &&
        ACTIVE_STATUSES.has(c.status ?? "") &&
        !isConversationKilled(c.id),
    );
    return hit ? `/${mode}/${hit.id}` : "/";
  } catch {
    return "/"; // list unavailable → fresh surface, never a dead click
  }
}

/** One icon per mode. A total Record (not a ternary) so a new mode is a compile
 * error here rather than silently inheriting another mode's glyph. */
const MODE_ICON: Record<Mode, LucideIcon | typeof CodeBlocksIcon> = {
  search: Search,
  build: CodeBlocksIcon,
  agent: Bot,
};

/**
 * The top-level mode slider (the ONLY mode control). A segmented rounded-pill
 * toggle holding the live modes — Search (grounded research), Build (the agent
 * surface framed for software), and Agent (the same machinery, general-task
 * framing). It is both the indicator (the filled segment shows the active mode)
 * and the selector. Dormant modes (if any) carry a SOON tag and don't switch.
 */
export function ModeSlider() {
  const { mode, setMode, modes } = useMode();
  const navigate = useNavigate();
  const location = useLocation();

  const switchTo = (next: Mode) => {
    if (next === mode && location.pathname === "/") return;
    setMode(next);
    // The route owns the rendered surface on /build/:cid etc. — switching the
    // slider must NAVIGATE (resume-or-new), or the click only recolors the pill
    // while the old conversation stays on screen.
    void resumeTargetFor(next).then((target) => {
      if (target !== location.pathname) navigate(target);
    });
  };

  return (
    <div
      role="radiogroup"
      aria-label="Mode"
      className="inline-flex items-center rounded-full border border-hairline bg-surface-1 p-px"
    >
      {modes.map((m) => {
        const active = m.id === mode;
        const Icon = MODE_ICON[m.id];
        return (
          <button
            key={m.id}
            type="button"
            role="radio"
            data-disco-control={`shell.mode-${m.id}`}
            data-active={active}
            aria-checked={active}
            aria-label={m.label}
            disabled={m.dormant}
            title={m.dormant ? `${m.label} — coming soon` : undefined}
            onClick={() => switchTo(m.id)}
            className={cn(
              // min-h-11 (44px, the shared mobile-spec floor) gives a real
              // touch target on phones; at lg+ it collapses back to the tight
              // desktop pill height (py-hair) — matches the app's lg: mobile
              // boundary rather than an earlier sm: cutoff that left the
              // 640–1024px range on the too-small 36px pill.
              "flex min-h-11 items-center gap-hair rounded-full px-body py-hair font-ui text-[0.8rem] transition-colors lg:min-h-0",
              active && "bg-surface-2 text-text",
              !active && !m.dormant && "text-text-muted hover:text-text",
              m.dormant && "cursor-not-allowed text-text-faint",
            )}
          >
            <Icon className="size-3.5 shrink-0" />
            {m.label}
            {m.dormant && (
              <span className="rounded-full border border-hairline px-1 text-[0.56rem] uppercase tracking-wide">
                soon
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}
