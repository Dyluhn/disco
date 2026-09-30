import { Menu } from "lucide-react";
import { Suspense, useEffect, useRef, useState } from "react";
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
  const openerRef = useRef<HTMLButtonElement>(null);
  const drawerRef = useRef<HTMLDivElement>(null);
  const desktopRailRef = useRef<HTMLElement>(null);
  const wasOpenRef = useRef(false);
  // Set when the open drawer is closed by a mobile->desktop breakpoint
  // crossing, so focus retargets to the surviving desktop navigation instead
  // of the mobile opener (which is hidden at lg+).
  const breakpointCloseRef = useRef(false);

  useEffect(() => {
    if (drawerOpen) {
      wasOpenRef.current = true;
      const firstLink = drawerRef.current?.querySelector("a[href]");
      if (firstLink instanceof HTMLElement) {
        firstLink.focus();
      }
    } else if (wasOpenRef.current) {
      wasOpenRef.current = false;
      if (breakpointCloseRef.current) {
        breakpointCloseRef.current = false;
        const target = desktopRailRef.current?.querySelector("a[href], button:not([disabled])");
        if (target instanceof HTMLElement) {
          target.focus();
          return;
        }
        // Desktop rail missing its controls: leave focus where the browser
        // placed it rather than forcing the hidden opener or BODY.
        return;
      }
      if (openerRef.current?.isConnected) {
        openerRef.current.focus();
      }
    }
  }, [drawerOpen]);

  // While the mobile drawer is open, a crossing to the desktop breakpoint
  // (Tailwind lg: 1024px) unmounts the overlay and retargets focus. Listeners
  // exist only while open and are removed on close/unmount, so a closed
  // drawer never opens or traps on resize.
  useEffect(() => {
    if (!drawerOpen) {
      return;
    }
    const closeForDesktop = () => {
      breakpointCloseRef.current = true;
      setDrawerOpen(false);
    };
    const onResize = () => {
      if (window.innerWidth >= 1024) {
        closeForDesktop();
      }
    };
    const onMediaChange = (event: { matches: boolean }) => {
      if (event.matches) {
        closeForDesktop();
      }
    };
    window.addEventListener("resize", onResize);
    let media: MediaQueryList | null = null;
    try {
      media = window.matchMedia("(min-width: 1024px)");
      if (typeof media.addEventListener === "function") {
        media.addEventListener("change", onMediaChange as (event: MediaQueryListEvent) => void);
      } else {
        (media as unknown as { addListener: (cb: typeof onMediaChange) => void }).addListener(
          onMediaChange,
        );
      }
    } catch {
      media = null;
    }
    return () => {
      window.removeEventListener("resize", onResize);
      try {
        if (media) {
          if (typeof media.removeEventListener === "function") {
            media.removeEventListener(
              "change",
              onMediaChange as (event: MediaQueryListEvent) => void,
            );
          } else {
            (
              media as unknown as { removeListener: (cb: typeof onMediaChange) => void }
            ).removeListener(onMediaChange);
          }
        }
      } catch {
        // Ignore cleanup errors; listeners are best-effort.
      }
    };
  }, [drawerOpen]);

  return (
    <div className="flex h-dvh overflow-hidden bg-bg text-text">
      <CommandPalette />
      {/* desktop rail — in-flow, width-collapsing */}
      <aside
        ref={desktopRailRef}
        className={cn(
          "hidden shrink-0 overflow-hidden transition-[width] duration-200 ease-out lg:block",
          collapsed ? "w-16" : "w-56",
        )}
      >
        <NavRail collapsed={collapsed} onToggleCollapse={() => setCollapsed((c) => !c)} />
      </aside>

      {/* mobile drawer + backdrop — overlays, never squeezes */}
      {drawerOpen && (
        <div
          ref={drawerRef}
          className="lg:hidden"
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              setDrawerOpen(false);
              return;
            }
            if (event.key !== "Tab") {
              return;
            }
            const root = drawerRef.current;
            if (!root) {
              return;
            }
            const focusables = Array.from(
              root.querySelectorAll<HTMLElement>(
                'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
              ),
            );
            if (focusables.length === 0) {
              event.preventDefault();
              return;
            }
            const first = focusables[0];
            const last = focusables[focusables.length - 1];
            const active = document.activeElement as HTMLElement | null;
            if (event.shiftKey) {
              if (active === first || !root.contains(active)) {
                event.preventDefault();
                last.focus();
              }
            } else if (active === last) {
              event.preventDefault();
              first.focus();
            }
          }}
        >
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
              ref={openerRef}
              type="button"
              data-disco-control="shell.drawer-open"
              aria-label="Open navigation"
              onClick={() => setDrawerOpen(true)}
              className="grid size-11 shrink-0 place-items-center rounded-control border border-hairline text-text-muted transition-colors hover:text-text lg:hidden"
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
