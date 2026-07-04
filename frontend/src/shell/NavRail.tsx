import {
  Activity,
  Boxes,
  Clock,
  FolderGit2,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  Settings,
  Workflow,
} from "lucide-react";
import type { ComponentType } from "react";
import { NavLink } from "react-router-dom";
import { cn } from "@/lib/cn";
import { useRunningCount } from "@/hooks/useActivity";
import { useMode } from "@/shell/mode";

interface NavItem {
  /** Stable, surface-scoped id for the test handle (independent of label copy). */
  id: string;
  to: string;
  label: string;
  icon: ComponentType<{ className?: string; "aria-hidden"?: boolean }>;
  /** Dormant items (Spaces) are present but not yet navigable. */
  dormant?: boolean;
  /** "New" is the home/start affordance; matches the index route exactly. */
  end?: boolean;
}

const ITEMS: NavItem[] = [
  { id: "new", to: "/", label: "New", icon: Plus, end: true },
  // Activity is the background-task dashboard — what's running now + scheduled-run
  // history. It carries the live "N running" badge (the global indicator).
  { id: "activity", to: "/activity", label: "Activity", icon: Activity },
  { id: "history", to: "/history", label: "History", icon: Clock },
  // Projects is its OWN surface — distinct from History (ephemeral research) and
  // Spaces (research corpora). This is for resumable Build workspaces.
  { id: "projects", to: "/projects", label: "Projects", icon: FolderGit2 },
  { id: "workflows", to: "/workflows", label: "Workflows", icon: Workflow },
  { id: "spaces", to: "/spaces", label: "Spaces", icon: Boxes, dormant: true },
  { id: "settings", to: "/settings", label: "Settings", icon: Settings },
];

interface Props {
  collapsed: boolean;
  onToggleCollapse: () => void;
  /** Called after a successful navigation (used to close the mobile drawer). */
  onNavigate?: () => void;
}

/**
 * The hideable left nav rail (Prompt 2). Quiet styling — hairline separation, the
 * UI grotesk, chroma ONLY on the active item. Collapsed → icon-only (labels kept
 * as accessible names); expanded → icon + label. Spaces is present-but-dormant.
 */
export function NavRail({ collapsed, onToggleCollapse, onNavigate }: Props) {
  const running = useRunningCount();
  const { setMode } = useMode();
  return (
    <nav
      aria-label="Primary"
      className="flex h-full flex-col gap-section border-r border-hairline bg-surface-1 py-section"
    >
      {/* wordmark — collapses to a serif monogram */}
      <div className={cn("flex items-center px-inline", collapsed ? "justify-center" : "px-body")}>
        <span className="font-display tracking-tight text-text" aria-label="Disco">
          {collapsed ? (
            <span className="text-[1.3rem]">D</span>
          ) : (
            <span className="text-[1.15rem]">Disco</span>
          )}
        </span>
      </div>

      {/* nav items */}
      <ul className="flex flex-1 flex-col gap-hair px-inline">
        {ITEMS.map((item) => {
          const Icon = item.icon;
          if (item.dormant) {
            return (
              <li key={item.to}>
                <span
                  data-disco-control={`shell.nav-${item.id}`}
                  data-dormant="true"
                  aria-disabled="true"
                  title="Coming soon"
                  className={cn(
                    "flex cursor-not-allowed items-center gap-inline rounded-control px-inline py-inline font-ui text-[0.86rem] text-text-faint",
                    collapsed && "justify-center",
                  )}
                >
                  <Icon className="size-4 shrink-0" aria-hidden />
                  {!collapsed && (
                    <>
                      <span className="flex-1">{item.label}</span>
                      <span className="rounded-[0.25rem] border border-hairline px-1 py-px text-[0.58rem] uppercase tracking-wide">
                        soon
                      </span>
                    </>
                  )}
                  {collapsed && <span className="sr-only">{item.label} (coming soon)</span>}
                </span>
              </li>
            );
          }
          // The Activity item carries the live "N running" badge — the global
          // indicator, visible from every screen (the rail is always mounted).
          const badge = item.to === "/activity" && running > 0 ? running : 0;
          return (
            <li key={item.to}>
              <NavLink
                to={item.to}
                end={item.end}
                data-disco-control={`shell.nav-${item.id}`}
                {...(item.to === "/activity" ? { "data-running-count": running } : {})}
                onClick={() => {
                  // "New" is the home/start affordance: reset the surface to the
                  // default landing mode (Search) so it doesn't pin the user to the
                  // surface they were last on (e.g. a Build). Other items navigate
                  // without touching the mode.
                  if (item.to === "/") setMode("search");
                  onNavigate?.();
                }}
                aria-label={badge > 0 ? `${item.label} (${badge} running)` : item.label}
                className={({ isActive }) =>
                  cn(
                    "relative flex items-center gap-inline rounded-control px-inline py-inline font-ui text-[0.86rem] transition-colors",
                    collapsed && "justify-center",
                    isActive
                      ? "bg-surface-2 text-accent"
                      : "text-text-muted hover:bg-surface-2 hover:text-text",
                  )
                }
              >
                <Icon className="size-4 shrink-0" aria-hidden />
                {!collapsed && <span className="flex-1">{item.label}</span>}
                {/* expanded → count pill; collapsed → a small accent dot on the icon */}
                {badge > 0 &&
                  (collapsed ? (
                    <span
                      aria-hidden
                      className="absolute right-1 top-1 size-2 rounded-full bg-accent"
                    />
                  ) : (
                    <span className="rounded-full bg-accent/15 px-1.5 font-ui text-[0.68rem] text-accent">
                      {badge}
                    </span>
                  ))}
              </NavLink>
            </li>
          );
        })}
      </ul>

      {/* collapse toggle — pinned to the rail foot */}
      <div className="px-inline">
        <button
          type="button"
          data-disco-control="shell.rail-collapse"
          onClick={onToggleCollapse}
          aria-label={collapsed ? "Expand navigation" : "Collapse navigation"}
          aria-pressed={collapsed}
          className={cn(
            "flex w-full items-center gap-inline rounded-control px-inline py-inline font-ui text-[0.82rem] text-text-muted transition-colors hover:bg-surface-2 hover:text-text",
            collapsed && "justify-center",
          )}
        >
          {collapsed ? (
            <PanelLeftOpen className="size-4 shrink-0" aria-hidden />
          ) : (
            <PanelLeftClose className="size-4 shrink-0" aria-hidden />
          )}
          {!collapsed && <span>Collapse</span>}
        </button>
      </div>
    </nav>
  );
}
