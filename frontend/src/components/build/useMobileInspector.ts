import { useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";

/** Focus and visibility for the mobile sheet; the inspector stays mounted once. */
export function useMobileInspector(rootRef: RefObject<HTMLDivElement | null>, collapsed: boolean) {
  const [desktop, setDesktop] = useState(() => window.matchMedia("(min-width: 1024px)").matches);
  const [open, setOpen] = useState(false);
  const chatRef = useRef<HTMLElement>(null);
  const inspectorRef = useRef<HTMLElement>(null);
  const openerRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const collapseRef = useRef<HTMLButtonElement>(null);
  const expandRef = useRef<HTMLButtonElement>(null);
  const previous = useRef({ desktop, open });
  const lastFocused = useRef<Element | null>(null);
  const active = !desktop && open;

  useEffect(() => {
    const media = window.matchMedia("(min-width: 1024px)");
    const rememberFocus = (event: FocusEvent) => {
      lastFocused.current = event.target instanceof Element ? event.target : null;
    };
    const releaseFocus = () => {
      // CSS can hide the focused control before the media change callback.
      // Keep that identity only across the pending breakpoint transition;
      // ordinary blur must not leave a stale inspector focus to restore later.
      if (media.matches === previous.current.desktop) lastFocused.current = null;
    };
    document.addEventListener("focusin", rememberFocus, true);
    document.addEventListener("focusout", releaseFocus, true);
    const update = () => {
      setDesktop(media.matches);
      // Returning to mobile starts on chat, not an old overlay.
      if (media.matches) setOpen(false);
    };
    update();
    media.addEventListener("change", update);
    return () => {
      media.removeEventListener("change", update);
      document.removeEventListener("focusin", rememberFocus, true);
      document.removeEventListener("focusout", releaseFocus, true);
    };
  }, []);

  useLayoutEffect(() => {
    const focused = document.activeElement === document.body
      && lastFocused.current?.isConnected
      ? lastFocused.current : document.activeElement;
    let target: HTMLElement | null = null;
    if (previous.current.desktop !== desktop) {
      if (desktop) {
        // Keep a focused inspector child when it remains visible inline.
        if (focused === openerRef.current || focused === closeRef.current
          || (collapsed && inspectorRef.current?.contains(focused))) {
          target = collapsed ? expandRef.current : collapseRef.current;
        }
      } else if (rootRef.current?.contains(focused) && !chatRef.current?.contains(focused)) {
        target = openerRef.current;
      }
    } else if (active && !previous.current.open) {
      target = closeRef.current;
    } else if (!desktop && !open && previous.current.open) {
      target = openerRef.current;
    }
    previous.current = { desktop, open };
    if (target?.isConnected && !target.matches(":disabled, [aria-disabled='true']") && !target.closest("[inert]")) {
      target.focus({ preventScroll: true });
    }
  }, [active, collapsed, desktop, open, rootRef]);

  useEffect(() => {
    if (!active) return;
    const dismiss = (event: KeyboardEvent) => {
      // Bubble after child handlers and Radix's document-level dismissals:
      // a nested menu/dialog or editor gets first refusal on Escape.
      if (event.key === "Escape" && !event.defaultPrevented && !event.isComposing) {
        event.preventDefault();
        setOpen(false);
      }
    };
    window.addEventListener("keydown", dismiss);
    return () => window.removeEventListener("keydown", dismiss);
  }, [active]);

  return { desktop, open, active, setOpen, chatRef, inspectorRef, openerRef, closeRef, collapseRef, expandRef };
}
