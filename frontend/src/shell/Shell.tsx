import * as Dialog from "@radix-ui/react-dialog";
import { Menu, X } from "lucide-react";
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
  const drawerRef = useRef<HTMLDivElement>(null);
  const desktopRailRef = useRef<HTMLElement>(null);
  // Set when the open drawer is closed by a mobile->desktop breakpoint
  // crossing, so focus retargets to the surviving desktop navigation instead
  // of the mobile opener (which is hidden at lg+).
  const breakpointCloseRef = useRef(false);

  // While the drawer is open, crossing to the desktop breakpoint (Tailwind lg:
  // 1024px) closes it. The listener exists only while open, so a closed drawer
  // never reacts to resizes.
  useEffect(() => {
    if (!drawerOpen) {
      return;
    }
    const desktop = window.matchMedia("(min-width: 1024px)");
    const closeOnDesktop = () => {
      if (!desktop.matches) return;
      breakpointCloseRef.current = true;
      setDrawerOpen(false);
    };
    desktop.addEventListener("change", closeOnDesktop);
    return () => desktop.removeEventListener("change", closeOnDesktop);
  }, [drawerOpen]);

  // After a breakpoint close, focus the surviving desktop navigation. The
  // dialog's own close focus (to the now-hidden opener) is suppressed below.
  useEffect(() => {
    if (drawerOpen || !breakpointCloseRef.current) {
      return;
    }
    const target = desktopRailRef.current?.querySelector("a[href], button:not([disabled])");
    if (target instanceof HTMLElement) {
      target.focus();
    }
  }, [drawerOpen]);

  return (
    <Dialog.Root open={drawerOpen} onOpenChange={setDrawerOpen}>
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
      <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-30 bg-black/45 lg:hidden" />
          <Dialog.Content
            ref={drawerRef}
            className="fixed inset-y-0 left-0 z-40 w-64 pmx-rise lg:hidden"
            onOpenAutoFocus={(event) => {
              // Land on the first navigation link (Radix skips links by default).
              const firstLink = drawerRef.current?.querySelector("a[href]");
              if (firstLink instanceof HTMLElement) {
                event.preventDefault();
                firstLink.focus();
              }
            }}
            onCloseAutoFocus={(event) => {
              if (breakpointCloseRef.current) {
                breakpointCloseRef.current = false;
                event.preventDefault();
              }
            }}
          >
            <Dialog.Title className="sr-only">Navigation</Dialog.Title>
            <Dialog.Description className="sr-only">Choose a Disco view.</Dialog.Description>
            <Dialog.Close
              data-disco-control="shell.drawer-close"
              aria-label="Close navigation"
              className="absolute right-3 top-3 z-50 grid size-11 place-items-center rounded-control text-text-muted hover:text-text"
            >
              <X className="size-4" aria-hidden />
            </Dialog.Close>
            <NavRail
              collapsed={false}
              onToggleCollapse={() => setDrawerOpen(false)}
              onNavigate={() => setDrawerOpen(false)}
            />
          </Dialog.Content>
      </Dialog.Portal>

      {/* main column */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex shrink-0 items-center justify-between gap-inline border-b border-hairline px-body py-inline">
          <div className="flex items-center gap-inline">
            <Dialog.Trigger
              type="button"
              data-disco-control="shell.drawer-open"
              aria-label="Open navigation"
              className="grid size-11 shrink-0 place-items-center rounded-control border border-hairline text-text-muted transition-colors hover:text-text lg:hidden"
            >
              <Menu className="size-4" aria-hidden />
            </Dialog.Trigger>
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
    </Dialog.Root>
  );
}
