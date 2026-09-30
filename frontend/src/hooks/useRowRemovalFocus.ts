import { useLayoutEffect, useRef, useState, type RefObject } from "react";

interface RemovalFocus {
  id: string;
  origin: HTMLButtonElement;
  order: readonly string[];
  succeeded: boolean;
}

function reachable(node: HTMLButtonElement | null | undefined): node is HTMLButtonElement {
  return Boolean(node?.isConnected && !node.disabled && !node.hidden
    && !node.closest("[hidden], [inert], [aria-hidden='true']")
    && getComputedStyle(node).display !== "none"
    && getComputedStyle(node).visibility !== "hidden");
}

/** Restore only the focused control removed by this successful mutation. */
export function useRowRemovalFocus(
  ids: readonly string[],
  fallback: RefObject<HTMLButtonElement | null>,
) {
  const buttons = useRef(new Map<string, HTMLButtonElement>());
  const pending = useRef<RemovalFocus | null>(null);
  const [settled, setSettled] = useState(0);

  const register = (id: string, node: HTMLButtonElement | null) => {
    if (node) buttons.current.set(id, node);
    else buttons.current.delete(id);
  };
  const begin = (id: string, origin: HTMLButtonElement) => {
    pending.current = document.activeElement === origin
      ? { id, origin, order: [...ids], succeeded: false }
      : null;
  };
  const cancel = (id: string) => {
    if (pending.current?.id === id) pending.current = null;
  };
  const leave = (id: string, origin: HTMLButtonElement) => {
    // A user blur while the control still exists relinquishes focus ownership.
    if (pending.current?.id === id && origin.isConnected) cancel(id);
  };
  const succeed = (id: string) => {
    if (pending.current?.id !== id) return;
    pending.current.succeeded = true;
    setSettled((value) => value + 1);
  };

  useLayoutEffect(() => {
    const request = pending.current;
    if (!request?.succeeded || ids.includes(request.id)) return;
    pending.current = null;
    if (request.origin.isConnected
      || (document.activeElement !== document.body
        && document.activeElement !== request.origin)) return;
    const position = request.order.indexOf(request.id);
    const candidates = [
      ...request.order.slice(position + 1),
      ...request.order.slice(0, position).reverse(),
      ...ids,
    ];
    const target = candidates.map((id) => buttons.current.get(id)).find(reachable)
      ?? fallback.current;
    if (reachable(target)) target.focus();
  }, [ids, fallback, settled]);

  return { register, begin, cancel, leave, succeed };
}
