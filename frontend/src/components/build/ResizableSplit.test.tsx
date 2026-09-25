import { useEffect, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import * as Menu from "@radix-ui/react-dropdown-menu";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ResizableSplit } from "./ResizableSplit";

const OPEN = "Open inspector — files, terminal, preview";
const CLOSE = "Close inspector — back to chat";
const COLLAPSE = "Collapse inspector pane — chat takes the full width";
let desktop = false;
const listeners = new Set<() => void>();
const media = {
  get matches() { return desktop; },
  media: "(min-width: 1024px)",
  addEventListener: (_: string, listener: () => void) => listeners.add(listener),
  removeEventListener: (_: string, listener: () => void) => listeners.delete(listener),
} as unknown as MediaQueryList;

function resize(wide: boolean) {
  act(() => {
    desktop = wide;
    for (const listener of listeners) listener();
  });
}

function fixture(onMount = vi.fn(), onUnmount = vi.fn()) {
  function Inspector() {
    const [count, setCount] = useState(0);
    useEffect(() => { onMount(); return onUnmount; }, []);
    return <>
      <button onClick={() => setCount(count + 1)}>Inspector count {count}</button>
      <input aria-label="Inspector editor" onKeyDown={(event) => {
        if (event.key === "Escape") event.preventDefault();
      }} />
      <Dialog.Root>
        <Dialog.Trigger>Nested dialog</Dialog.Trigger>
        <Dialog.Portal><Dialog.Content>
          <Dialog.Title>Inspector details</Dialog.Title>
          <Dialog.Description>Details of this artifact</Dialog.Description>
          <Dialog.Close>Close details</Dialog.Close>
        </Dialog.Content></Dialog.Portal>
      </Dialog.Root>
      <Menu.Root>
        <Menu.Trigger>Nested menu</Menu.Trigger>
        <Menu.Portal><Menu.Content><Menu.Item>Menu action</Menu.Item></Menu.Content></Menu.Portal>
      </Menu.Root>
    </>;
  }
  return render(<>
    <nav aria-label="Shell"><button>Shell navigation</button></nav>
    <ResizableSplit
      chat={<><button>Download source</button><input aria-label="Chat input" /></>}
      inspector={<Inspector />}
    />
  </>);
}

beforeEach(() => {
  desktop = false;
  listeners.clear();
  localStorage.clear();
  vi.spyOn(window, "matchMedia").mockReturnValue(media);
});
afterEach(() => vi.restoreAllMocks());

async function openInspector() {
  const user = userEvent.setup();
  const opener = screen.getByRole("button", { name: OPEN });
  opener.focus();
  await user.keyboard("{Enter}");
  expect(screen.getByRole("button", { name: CLOSE })).toHaveFocus();
  return { user, opener, sheet: screen.getByRole("region", { name: "Inspector" }) };
}

describe("ResizableSplit mobile inspector", () => {
  it("moves keyboard focus into the sheet and makes only covered chat inert", async () => {
    fixture();
    const { opener, sheet } = await openInspector();
    expect(opener).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("textbox", { name: "Chat input" }).closest("[inert]")).not.toBeNull();
    expect(sheet).not.toHaveAttribute("inert");
    const shell = screen.getByRole("button", { name: "Shell navigation" });
    expect(shell.closest("[inert]")).toBeNull();
    shell.focus();
    expect(shell).toHaveFocus(); // The sheet must not trap the whole shell.
  });

  it.each(["Escape", "Close"])("restores the opener and chat access after %s", async (dismissal) => {
    fixture();
    const { user, opener, sheet } = await openInspector();
    if (dismissal === "Escape") await user.keyboard("{Escape}");
    else await user.click(screen.getByRole("button", { name: CLOSE }));
    expect(opener).toHaveFocus();
    expect(opener).toHaveAttribute("aria-expanded", "false");
    expect(opener.closest("[inert]")).toBeNull();
    expect(sheet).toHaveAttribute("inert");
    await user.keyboard("{Enter}");
    expect(screen.getByRole("button", { name: CLOSE })).toHaveFocus();
  });

  it.each(["dialog", "menu"])("lets a nested Radix %s consume Escape before dismissing the sheet", async (kind) => {
    fixture();
    const { user, opener, sheet } = await openInspector();
    const nestedTrigger = within(sheet).getByRole("button", { name: `Nested ${kind}` });
    await user.click(nestedTrigger);
    await screen.findByRole(kind);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole(kind)).not.toBeInTheDocument());
    await waitFor(() => expect(nestedTrigger).toHaveFocus());
    expect(opener).toHaveAttribute("aria-expanded", "true");
    expect(sheet).not.toHaveAttribute("inert");
    await user.keyboard("{Escape}");
    expect(opener).toHaveFocus();
    expect(opener).toHaveAttribute("aria-expanded", "false");
  });

  it("respects an editor's consumed Escape and removes listeners on unmount", async () => {
    const view = fixture();
    const { user, opener } = await openInspector();
    const editor = screen.getByRole("textbox", { name: "Inspector editor" });
    editor.focus();
    await user.keyboard("{Escape}");
    expect(editor).toHaveFocus();
    expect(opener).toHaveAttribute("aria-expanded", "true");
    view.unmount();
    expect(listeners.size).toBe(0);
    const event = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
    window.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(false);
  });

  it("keeps one inspector mount and its state across closing and both breakpoints", async () => {
    const mounted = vi.fn();
    const unmounted = vi.fn();
    const view = fixture(mounted, unmounted);
    const { user } = await openInspector();
    await user.click(screen.getByRole("button", { name: "Inspector count 0" }));
    await user.keyboard("{Escape}");
    await user.keyboard("{Enter}");
    expect(screen.getByRole("button", { name: "Inspector count 1" })).toBeInTheDocument();
    resize(true);
    resize(false);
    expect(mounted).toHaveBeenCalledTimes(1);
    expect(unmounted).not.toHaveBeenCalled();
    expect(screen.getAllByRole("button", { name: "Inspector count 1" })).toHaveLength(1);
    view.unmount();
    expect(unmounted).toHaveBeenCalledTimes(1);
  });

  it("moves a mobile-only Close control to the desktop collapse control on resize", async () => {
    fixture();
    const { opener, sheet } = await openInspector();
    resize(true);
    expect(screen.getByRole("button", { name: COLLAPSE })).toHaveFocus();
    expect(opener.closest("[inert]")).toBeNull();
    expect(sheet).not.toHaveAttribute("inert");
    resize(false);
    expect(opener).toHaveFocus();
    expect(opener).toHaveAttribute("aria-expanded", "false");
    expect(sheet).toHaveAttribute("inert");
  });

  it("keeps a still-visible inspector child's focus on desktop and restores mobile chat on return", async () => {
    fixture();
    const { opener, sheet } = await openInspector();
    const editor = screen.getByRole("textbox", { name: "Inspector editor" });
    editor.focus();
    resize(true);
    expect(editor).toHaveFocus();
    resize(false);
    expect(opener).toHaveFocus();
    expect(sheet).toHaveAttribute("inert");
  });

  it("honors persisted desktop collapse when leaving mobile and focuses the surviving expand control", async () => {
    localStorage.setItem("pmx.build.inspectorCollapsed", "1");
    fixture();
    const { opener, sheet } = await openInspector();
    resize(true);
    expect(screen.getByRole("button", { name: "Show inspector pane" })).toHaveFocus();
    expect(sheet).toHaveAttribute("inert");
    expect(localStorage.getItem("pmx.build.inspectorCollapsed")).toBe("1");
    resize(false);
    expect(opener).toHaveFocus();
  });

  it("does not move shell or chat focus when the viewport changes", async () => {
    fixture();
    const { user } = await openInspector();
    const shell = screen.getByRole("button", { name: "Shell navigation" });
    shell.focus();
    resize(true);
    expect(shell).toHaveFocus();
    const chat = screen.getByRole("textbox", { name: "Chat input" });
    await user.click(chat);
    resize(false);
    expect(chat).toHaveFocus();
  });

  it("preserves desktop width clamps, pointer cleanup, and collapse persistence", () => {
    desktop = true;
    const view = fixture();
    const sheet = screen.getByRole("region", { name: "Inspector" });
    const container = sheet.parentElement!;
    vi.spyOn(container, "getBoundingClientRect").mockReturnValue({ width: 1000, right: 1000 } as DOMRect);
    const handle = screen.getByRole("separator", { name: "Resize inspector pane" });
    fireEvent.pointerDown(handle);
    // jsdom lacks PointerEvent coordinates; a native MouseEvent carries the
    // same clientX field consumed by the production pointermove listener.
    fireEvent(window, new MouseEvent("pointermove", { clientX: 950 }));
    expect(parseFloat(sheet.style.getPropertyValue("--ins-w"))).toBeCloseTo(28);
    fireEvent(window, new MouseEvent("pointermove", { clientX: 0 }));
    expect(sheet.style.getPropertyValue("--ins-w")).toBe("68%");
    fireEvent.pointerUp(window);
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");
    fireEvent.click(screen.getByRole("button", { name: COLLAPSE }));
    expect(localStorage.getItem("pmx.build.inspectorCollapsed")).toBe("1");
    expect(sheet.style.getPropertyValue("--ins-w")).toBe("0px");
    fireEvent.click(screen.getByRole("button", { name: "Show inspector pane" }));
    expect(localStorage.getItem("pmx.build.inspectorCollapsed")).toBe("0");
    expect(sheet.style.getPropertyValue("--ins-w")).toBe("68%");
    fireEvent.pointerDown(screen.getByRole("separator"));
    view.unmount();
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");
  });
});

describe("ResizableSplit keyboard resizing", () => {
  function desktopFixture(actualInitialWidth?: number) {
    desktop = true;
    let notify = () => {};
    const observe = vi.fn();
    const disconnect = vi.fn();
    vi.stubGlobal("ResizeObserver", class {
      constructor(callback: () => void) { notify = callback; }
      observe = observe;
      disconnect = disconnect;
    });
    const view = fixture();
    const sheet = screen.getByRole("region", { name: "Inspector" });
    const container = sheet.parentElement!;
    const initialStyle = sheet.style.getPropertyValue("--ins-w");
    let total = 1000;
    const readContainer = vi.spyOn(container, "getBoundingClientRect").mockImplementation(() => ({ width: total, right: total } as DOMRect));
    vi.spyOn(sheet, "getBoundingClientRect").mockImplementation(() => {
      const style = sheet.style.getPropertyValue("--ins-w");
      const width = style === initialStyle && actualInitialWidth !== undefined
        ? actualInitialWidth
        : Math.min(Math.max(total * parseFloat(style) / 100, 280), Math.max(total - 320, 280));
      return { width: total > 0 ? width : 0 } as DOMRect;
    });
    act(notify);
    return {
      ...view, sheet, container, observe, disconnect, readContainer,
      handle: screen.getByRole("separator", { name: "Resize inspector pane" }),
      resizeContainer(width: number, viaWindow = false) {
        total = width;
        act(() => { if (viaWindow) window.dispatchEvent(new Event("resize")); else notify(); });
      },
    };
  }

  afterEach(() => vi.unstubAllGlobals());

  it("uses actual rendered width for arrow keys and exposes current pixel ARIA values", async () => {
    const user = userEvent.setup();
    const { handle } = desktopFixture(460); // Saved 58% is not the rendered width.
    expect(handle).toHaveAttribute("aria-valuenow", "460");
    expect(handle).toHaveAttribute("aria-valuemin", "280");
    expect(handle).toHaveAttribute("aria-valuemax", "680");
    handle.focus();
    await user.keyboard("{ArrowLeft}{ArrowLeft}{ArrowRight}");
    expect(handle).toHaveFocus();
    expect(handle).toHaveAttribute("aria-valuenow", "484");
    expect(handle).toHaveAttribute("aria-valuetext", "484 pixels inspector width");
    expect(Number(localStorage.getItem("pmx.build.inspectorWidth"))).toBeCloseTo(0.484);
  });

  it("clamps Home, End, and repeated arrows to pointer limits and persists across remount", async () => {
    const user = userEvent.setup();
    const first = desktopFixture();
    first.handle.focus();
    await user.keyboard("{Home}{ArrowRight}{ArrowRight}");
    expect(first.handle).toHaveAttribute("aria-valuenow", "280");
    await user.keyboard("{End}{ArrowLeft}{ArrowLeft}");
    expect(first.handle).toHaveAttribute("aria-valuenow", "680");
    expect(Number(localStorage.getItem("pmx.build.inspectorWidth"))).toBeCloseTo(0.68);
    first.unmount();
    const second = desktopFixture();
    expect(second.handle).toHaveAttribute("aria-valuenow", "680");
    expect(parseFloat(second.sheet.style.getPropertyValue("--ins-w"))).toBeCloseTo(68);
  });

  it("refreshes bounds for container and window resizing and cleans both subscriptions", () => {
    const view = desktopFixture();
    expect(view.observe).toHaveBeenCalledWith(view.container);
    expect(view.observe).toHaveBeenCalledWith(view.sheet);
    view.resizeContainer(800);
    expect(view.handle).toHaveAttribute("aria-valuenow", "464");
    expect(view.handle).toHaveAttribute("aria-valuemax", "480");
    view.resizeContainer(1200, true);
    expect(view.handle).toHaveAttribute("aria-valuenow", "696");
    expect(view.handle).toHaveAttribute("aria-valuemax", "880");
    view.resizeContainer(500);
    expect(view.handle).toHaveAttribute("aria-valuenow", "280");
    expect(view.handle).toHaveAttribute("aria-valuemin", "280");
    expect(view.handle).toHaveAttribute("aria-valuemax", "280");
    view.unmount();
    expect(view.disconnect).toHaveBeenCalledTimes(1);
    view.readContainer.mockClear();
    window.dispatchEvent(new Event("resize"));
    expect(view.readContainer).not.toHaveBeenCalled();
  });

  it("does not consume unrelated shortcuts or write invalid geometry and restores focus on mobile resize", async () => {
    const user = userEvent.setup();
    const view = desktopFixture();
    view.handle.focus();
    const original = localStorage.getItem("pmx.build.inspectorWidth");
    expect(fireEvent.keyDown(view.handle, { key: "ArrowLeft", ctrlKey: true })).toBe(true);
    expect(fireEvent.keyDown(view.handle, { key: "ArrowDown" })).toBe(true);
    view.resizeContainer(0);
    expect(fireEvent.keyDown(view.handle, { key: "End" })).toBe(true);
    expect(localStorage.getItem("pmx.build.inspectorWidth")).toBe(original);
    expect(view.handle).not.toHaveAttribute("aria-valuenow");
    view.resizeContainer(1000);
    await user.keyboard("{ArrowLeft}");
    expect(view.handle).toHaveAttribute("aria-valuenow", "604");
    resize(false);
    expect(screen.getByRole("button", { name: OPEN })).toHaveFocus();
    expect(view.sheet).toHaveAttribute("inert");
  });
});
