import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { ModeProvider } from "./ModeProvider";
import { Shell } from "./Shell";

function renderShell(initial = "/") {
  // The Shell is always mounted under QueryClientProvider in production (App.tsx);
  // the NavRail's live "N running" badge reads a query, so mirror that here.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initial]}>
        <ModeProvider>
          <Routes>
            <Route element={<Shell />}>
              <Route index element={<div>Home surface</div>} />
              <Route path="history" element={<div>History view content</div>} />
              <Route path="settings" element={<div>Settings view content</div>} />
              <Route path="*" element={<div>Home surface</div>} />
            </Route>
          </Routes>
        </ModeProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Application shell", () => {
  it("renders the nav rail, mode indicator, and theme toggle in the chrome", () => {
    renderShell();
    // Nav items are real links (chroma only on active — not asserted here).
    expect(screen.getByRole("link", { name: "New" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "History" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Settings" })).toBeInTheDocument();
    // Theme toggle relocated into the shell chrome.
    expect(screen.getByRole("button", { name: /switch to (light|dark)/i })).toBeInTheDocument();
  });

  it("shows Spaces as a live route", () => {
    renderShell();
    expect(screen.getByRole("link", { name: "Spaces" })).toHaveAttribute("href", "/spaces");
  });

  it("uses a Search/Build slider as the sole mode control (no separate indicator, no Deep Research at top)", () => {
    renderShell();
    const group = screen.getByRole("radiogroup", { name: "Mode" });
    expect(within(group).getByRole("radio", { name: "search" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    // Build is now WOKEN — a live, selectable mode (no longer dormant/SOON).
    expect(within(group).getByRole("radio", { name: "build" })).toBeEnabled();
    // No separate/duplicate mode indicator; Deep Research is not a top-level mode.
    expect(screen.queryByRole("status", { name: /mode/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/Deep Research/i)).not.toBeInTheDocument();
  });

  it("routes between views via the rail", async () => {
    const user = userEvent.setup();
    renderShell();
    expect(screen.getByText("Home surface")).toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "History" }));
    expect(screen.getByText("History view content")).toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "Settings" }));
    expect(screen.getByText("Settings view content")).toBeInTheDocument();
  });

  it("collapses and expands the rail", async () => {
    const user = userEvent.setup();
    renderShell();
    expect(screen.getByRole("button", { name: "Collapse navigation" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Collapse navigation" }));
    // After collapse, the toggle flips to "Expand navigation" (icon-only rail).
    expect(screen.getByRole("button", { name: "Expand navigation" })).toBeInTheDocument();
  });

  it("opens and closes the mobile drawer without squeezing content", async () => {
    const user = userEvent.setup();
    renderShell();
    await user.click(screen.getByRole("button", { name: "Open navigation" }));
    // The drawer overlay exposes a backdrop close affordance.
    const close = screen.getByRole("button", { name: "Close navigation" });
    expect(close).toBeInTheDocument();
    await user.click(close);
    expect(screen.queryByRole("button", { name: "Close navigation" })).not.toBeInTheDocument();
  });
});

describe("mobile drawer focus management", () => {
  it("transfers focus to the first drawer link on open", async () => {
    const user = userEvent.setup();
    renderShell();
    const opener = screen.getByRole("button", { name: "Open navigation" });
    await user.click(opener);
    const backdropClose = screen.getByRole("button", { name: "Close navigation" });
    const drawer = backdropClose.parentElement as HTMLElement;
    expect(within(drawer).getByRole("link", { name: "New" })).toHaveFocus();
  });

  it("closes the drawer on Escape while focus is inside", async () => {
    const user = userEvent.setup();
    renderShell();
    await user.click(screen.getByRole("button", { name: "Open navigation" }));
    const backdropClose = screen.getByRole("button", { name: "Close navigation" });
    const drawer = backdropClose.parentElement as HTMLElement;
    within(drawer).getByRole("link", { name: "New" }).focus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("button", { name: "Close navigation" })).not.toBeInTheDocument();
  });

  it("restores focus to the opener on Escape from inside", async () => {
    const user = userEvent.setup();
    renderShell();
    const opener = screen.getByRole("button", { name: "Open navigation" });
    await user.click(opener);
    const backdropClose = screen.getByRole("button", { name: "Close navigation" });
    const drawer = backdropClose.parentElement as HTMLElement;
    within(drawer).getByRole("link", { name: "New" }).focus();
    await user.keyboard("{Escape}");
    expect(opener).toHaveFocus();
  });

  it("restores focus to the opener on backdrop close", async () => {
    const user = userEvent.setup();
    renderShell();
    const opener = screen.getByRole("button", { name: "Open navigation" });
    await user.click(opener);
    await user.click(screen.getByRole("button", { name: "Close navigation" }));
    expect(screen.queryByRole("button", { name: "Close navigation" })).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it("closes the drawer and restores focus to the opener on drawer collapse", async () => {
    const user = userEvent.setup();
    renderShell();
    const opener = screen.getByRole("button", { name: "Open navigation" });
    await user.click(opener);
    const backdropClose = screen.getByRole("button", { name: "Close navigation" });
    const drawer = backdropClose.parentElement as HTMLElement;
    await user.click(within(drawer).getByRole("button", { name: "Collapse navigation" }));
    expect(screen.queryByRole("button", { name: "Close navigation" })).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it("has no effect on Escape while closed", async () => {
    const user = userEvent.setup();
    renderShell();
    const opener = screen.getByRole("button", { name: "Open navigation" });
    opener.focus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("button", { name: "Close navigation" })).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it("navigates via a drawer link and closes the drawer", async () => {
    const user = userEvent.setup();
    renderShell();
    expect(screen.getByText("Home surface")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open navigation" }));
    const backdropClose = screen.getByRole("button", { name: "Close navigation" });
    const drawer = backdropClose.parentElement as HTMLElement;
    await user.click(within(drawer).getByRole("link", { name: "History" }));
    expect(screen.getByText("History view content")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Close navigation" })).not.toBeInTheDocument();
  });
});

describe("mobile drawer keyboard boundaries and responsive closure (jsdom-bounded)", () => {
  // JSDOM has no Tailwind lg: layout (existing vite css:true does not apply real
  // visibility). Keyboard tests therefore scope the rendered drawer DOM and use
  // real userEvent.keyboard Tab order; resize tests use bounded innerWidth +
  // resize plus a local matchMedia fixture. This proves JS closure and focus
  // retargeting only — never real CSS hiding — and asserts only user-visible
  // outcomes (mounted controls + document.activeElement), never hook/ref names.
  // No role=dialog is assumed: the drawer exposes no such role today.
  it("keeps forward Tab inside the drawer from the final Collapse control", async () => {
    const user = userEvent.setup();
    renderShell();
    const opener = screen.getByRole("button", { name: "Open navigation" });
    await user.click(opener);
    const backdropClose = screen.getByRole("button", { name: "Close navigation" });
    const drawer = backdropClose.parentElement as HTMLElement;
    const collapse = within(drawer).getByRole("button", { name: "Collapse navigation" });
    collapse.focus();
    expect(collapse).toHaveFocus();
    await user.keyboard("{Tab}");
    // User-visible: overlay stays mounted and focus stays on a mounted drawer
    // control — never the outside opener or background mode controls.
    expect(screen.getByRole("button", { name: "Close navigation" })).toBeInTheDocument();
    expect(opener).not.toHaveFocus();
    expect(drawer.contains(document.activeElement)).toBe(true);
    expect(["A", "BUTTON"]).toContain(document.activeElement?.tagName);
  });

  it("keeps Shift+Tab inside the drawer from the first control", async () => {
    const user = userEvent.setup();
    renderShell();
    const opener = screen.getByRole("button", { name: "Open navigation" });
    await user.click(opener);
    const backdropClose = screen.getByRole("button", { name: "Close navigation" });
    const drawer = backdropClose.parentElement as HTMLElement;
    // First tabbable in current DOM order is the backdrop Close control.
    // Starting there proves the boundary: starting from New would land on Close
    // (still inside) and pass trivially without any trap.
    backdropClose.focus();
    expect(backdropClose).toHaveFocus();
    await user.keyboard("{Shift>}{Tab}{/Shift}");
    expect(screen.getByRole("button", { name: "Close navigation" })).toBeInTheDocument();
    expect(opener).not.toHaveFocus();
    expect(drawer.contains(document.activeElement)).toBe(true);
  });

  it("closes the overlay on mobile->desktop crossing and moves focus to desktop nav", async () => {
    const user = userEvent.setup();
    const originalInnerWidth = window.innerWidth;
    const originalMatchMedia = window.matchMedia;
    const changeListeners = new Set<(e: { matches: boolean; media: string }) => void>();
    try {
      // Bounded mobile start (480px defect width), then desktop crossing (1460px).
      Object.defineProperty(window, "innerWidth", {
        writable: true,
        configurable: true,
        value: 480,
      });
      window.matchMedia = ((query: string) =>
        ({
          get matches() {
            return query.includes("min-width") ? window.innerWidth >= 1024 : false;
          },
          media: query,
          onchange: null,
          addEventListener: (type: string, cb: (e: { matches: boolean; media: string }) => void) => {
            if (type === "change") changeListeners.add(cb);
          },
          removeEventListener: (type: string, cb: (e: { matches: boolean; media: string }) => void) => {
            if (type === "change") changeListeners.delete(cb);
          },
          addListener: (cb: (e: { matches: boolean; media: string }) => void) => {
            changeListeners.add(cb);
          },
          removeListener: (cb: (e: { matches: boolean; media: string }) => void) => {
            changeListeners.delete(cb);
          },
          dispatchEvent: () => false,
        }) as unknown as MediaQueryList) as typeof window.matchMedia;
      renderShell();
      const opener = screen.getByRole("button", { name: "Open navigation" });
      await user.click(opener);
      expect(screen.getByRole("button", { name: "Close navigation" })).toBeInTheDocument();
      Object.defineProperty(window, "innerWidth", {
        writable: true,
        configurable: true,
        value: 1460,
      });
      act(() => {
        window.dispatchEvent(new Event("resize"));
        // Also cover matchMedia-change implementations with the same crossing.
        changeListeners.forEach((cb) => cb({ matches: true, media: "(min-width: 1024px)" }));
      });
      // User-visible: overlay unmounts (Close affordance gone).
      expect(screen.queryByRole("button", { name: "Close navigation" })).not.toBeInTheDocument();
      // Meaningful desktop focus: inside the remaining Primary nav, never BODY
      // and never the mobile opener (lg:hidden on desktop).
      const desktopNav = screen.getByRole("navigation", { name: "Primary" });
      expect(document.activeElement).not.toBe(document.body);
      expect(opener).not.toHaveFocus();
      expect(desktopNav.contains(document.activeElement)).toBe(true);
    } finally {
      Object.defineProperty(window, "innerWidth", {
        writable: true,
        configurable: true,
        value: originalInnerWidth,
      });
      window.matchMedia = originalMatchMedia;
    }
  });

  it("Tab while closed reaches background mode controls (no trap when shut)", async () => {
    const user = userEvent.setup();
    renderShell();
    const opener = screen.getByRole("button", { name: "Open navigation" });
    opener.focus();
    await user.keyboard("{Tab}");
    // Negative control: no overlay appears and focus leaves the opener for
    // background controls, proving any trap is conditional on open.
    expect(screen.queryByRole("button", { name: "Close navigation" })).not.toBeInTheDocument();
    expect(opener).not.toHaveFocus();
    expect(screen.getByRole("radiogroup", { name: "Mode" }).contains(document.activeElement)).toBe(true);
  });

  it("resize while closed stays closed", async () => {
    const originalInnerWidth = window.innerWidth;
    try {
      Object.defineProperty(window, "innerWidth", {
        writable: true,
        configurable: true,
        value: 480,
      });
      renderShell();
      const opener = screen.getByRole("button", { name: "Open navigation" });
      opener.focus();
      Object.defineProperty(window, "innerWidth", {
        writable: true,
        configurable: true,
        value: 1460,
      });
      act(() => window.dispatchEvent(new Event("resize")));
      // Negative control: a crossing with no open drawer opens nothing and
      // drops focus nowhere (never BODY).
      expect(screen.queryByRole("button", { name: "Close navigation" })).not.toBeInTheDocument();
      expect(document.activeElement).not.toBe(document.body);
    } finally {
      Object.defineProperty(window, "innerWidth", {
        writable: true,
        configurable: true,
        value: originalInnerWidth,
      });
    }
  });
});
