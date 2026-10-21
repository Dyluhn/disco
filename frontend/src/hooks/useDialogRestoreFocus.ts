import { useRef, type RefObject } from "react";

/** Restore a controlled dialog's actual invoker when it has no Radix Trigger.
 * A shared target keeps the original invoker across a sequence of dialogs. */
export function useDialogRestoreFocus(
  returnFocusRef?: RefObject<HTMLElement | null>,
  skipRestoreRef?: RefObject<boolean>,
) {
  const capturedInvoker = useRef<HTMLElement | null>(null);
  return {
    onOpenAutoFocus: () => {
      capturedInvoker.current = document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    },
    onCloseAutoFocus: (event: Event) => {
      event.preventDefault();
      if (skipRestoreRef?.current) return;
      const target = returnFocusRef ? returnFocusRef.current : capturedInvoker.current;
      // Generation can remove the button or disable its containing controls.
      if (target?.isConnected && !target.matches(":disabled, [aria-disabled='true']")
        && !target.closest("[hidden], [inert]")) {
        const style = target.ownerDocument.defaultView?.getComputedStyle(target);
        if (style?.display === "none" || style?.visibility === "hidden" || style?.visibility === "collapse") return;
        target.focus({ preventScroll: true });
      }
    },
  };
}
