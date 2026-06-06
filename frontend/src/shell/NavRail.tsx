import {
  Boxes,
  Clock,
  FolderGit2,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  Settings,
} from "lucide-react";
import type { ComponentType } from "react";
import { NavLink } from "react-router-dom";
import { cn } from "@/lib/cn";

interface NavItem {
  to: string;
  label: string;
  icon: ComponentType<{ className?: string; "aria-hidden"?: boolean }>;
  /** Dormant items (Spaces) are present but not yet navigable. */
  dormant?: boolean;
  /** "New" is the home/start affordance; matches the index route exactly. */
  end?: boolean;
}

const ITEMS: NavItem[] = [
  { to: "/", label: "New", icon: Plus, end: true },
  { to: "/history", label: "History", icon: Clock },
  // Projects is its OWN surface — distinct from History (ephemeral research) and
  // Spaces (research corpora). This is for resumable Build workspaces.
  { to: "/projects", label: "Projects", icon: FolderGit2 },
  { to: "/spaces", label: "Spaces", icon: Boxes, dormant: true },
  { to: "/settings", label: "Settings", icon: Settings },
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
  return (
    <nav
      aria-label="Primary"
      className="flex h-full flex-col gap-section border-r border-hairline bg-surface-1 py-section"
    >
      {/* wordmark — collapses to a serif monogram */}
      <div className={cn("flex items-center px-inline", collapsed ? "justify-center" : "px-body")}>
        <span className="font-display tracking-tight text-text" aria-label="perpleximanus">
          {collapsed ? (
            <span className="text-[1.3rem]">p</span>
          ) : (
            <span className="text-[1.15rem]">perpleximanus</span>
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
          return (
            <li key={item.to}>
              <NavLink
                to={item.to}
                end={item.end}
                onClick={onNavigate}
                aria-label={item.label}
                className={({ isActive }) =>
                  cn(
                    "flex items-center gap-inline rounded-control px-inline py-inline font-ui text-[0.86rem] transition-colors",
                    collapsed && "justify-center",
                    isActive
                      ? "bg-surface-2 text-accent"
                      : "text-text-muted hover:bg-surface-2 hover:text-text",
                  )
                }
              >
                <Icon className="size-4 shrink-0" aria-hidden />
                {!collapsed && <span className="flex-1">{item.label}</span>}
              </NavLink>
            </li>
          );
        })}
      </ul>

      {/* collapse toggle — pinned to the rail foot */}
      <div className="px-inline">
        <button
          type="button"
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
