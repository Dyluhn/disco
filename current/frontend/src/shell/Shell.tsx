import { Menu } from "lucide-react";
import { Suspense, useState } from "react";
import { Outlet } from "react-router-dom";
import { DemoDataBadge } from "@/components/DemoDataBadge";
import { SandboxHealthBanner } from "@/components/SandboxHealthBanner";
import { ThemeToggle } from "@/components/ThemeToggle";
import { CommandPalette } from "@/components/CommandPalette";
import { cn } from "@/lib/cn";
import { ModeSlider } from "./ModeSlider";
import { NavRail } from "./NavRail";

/**
 * The application shell (Prompt 2): the persistent frame that hosts every routed
 * view via <Outlet/>. A hideable left rail, a top chrome bar with the always-
 * visible mode indicator + theme toggle, and a scrollable main region.
 *
 * Responsive: on lg+ the rail is in-flow and collapses by WIDTH (smooth, no
 * content jump); on narrow viewports it becomes an overlay DRAWER so it never
 * squeezes the content. All shell state is session-local React state (no browser
 * storage, per the data discipline).
 */
export function Shell() {
  const [collapsed, setCollapsed] = useState(false); // desktop rail width
  const [drawerOpen, setDrawerOpen] = useState(false); // mobile overlay

  return (
    <div className="flex h-dvh overflow-hidden bg-bg text-text">
      <CommandPalette />
      {/* desktop rail — in-flow, width-collapsing */}
      <aside
        className={cn(
          "hidden shrink-0 overflow-hidden transition-[width] duration-200 ease-out lg:block",
          collapsed ? "w-16" : "w-56",
        )}
      >
        <NavRail collapsed={collapsed} onToggleCollapse={() => setCollapsed((c) => !c)} />
      </aside>

      {/* mobile drawer + backdrop — overlays, never squeezes */}
      {drawerOpen && (
        <div className="lg:hidden">
          <button
            type="button"
            data-disco-control="shell.drawer-close"
            aria-label="Close navigation"
            onClick={() => setDrawerOpen(false)}
            className="fixed inset-0 z-30 bg-black/45"
          />
          <div className="fixed inset-y-0 left-0 z-40 w-64 pmx-rise">
            <NavRail
              collapsed={false}
              onToggleCollapse={() => setDrawerOpen(false)}
              onNavigate={() => setDrawerOpen(false)}
            />
          </div>
        </div>
      )}

      {/* main column */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex shrink-0 items-center justify-between gap-inline border-b border-hairline px-body py-inline">
          <div className="flex items-center gap-inline">
            <button
              type="button"
              data-disco-control="shell.drawer-open"
              aria-label="Open navigation"
              onClick={() => setDrawerOpen(true)}
              className="grid size-9 place-items-center rounded-control border border-hairline text-text-muted transition-colors hover:text-text lg:hidden"
            >
              <Menu className="size-4" aria-hidden />
            </button>
            <ModeSlider />
          </div>
          <ThemeToggle />
        </header>

        <DemoDataBadge />
        <SandboxHealthBanner />

        <main className="min-h-0 flex-1 overflow-y-auto">
          <Suspense
            fallback={
              <div role="status" className="p-body text-sm text-text-muted">
                Loading view…
              </div>
            }
          >
            <Outlet />
          </Suspense>
        </main>
      </div>
    </div>
  );
}
