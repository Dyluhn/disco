import { useEffect, useState, type RefObject } from "react";

/**
 * Shared mobile primitive — pairs with `ScrollFadeEdges` in
 * `@/components/ScrollFade`. See that file's doc comment for full usage; this
 * half just tracks whether the given scroll container has hidden content to
 * the left/right.
 */
export function useScrollFade(ref: RefObject<HTMLElement | null>) {
  const [showLeft, setShowLeft] = useState(false);
  const [showRight, setShowRight] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const update = () => {
      setShowLeft(el.scrollLeft > 2);
      setShowRight(el.scrollLeft + el.clientWidth < el.scrollWidth - 2);
    };
    update();
    el.addEventListener("scroll", update, { passive: true });
    // jsdom (vitest) doesn't implement ResizeObserver — match the same
    // feature-detect the build editor's canvas already uses rather than
    // crashing every test that mounts this container.
    const ro = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(update);
    ro?.observe(el);
    window.addEventListener("resize", update);
    return () => {
      el.removeEventListener("scroll", update);
      ro?.disconnect();
      window.removeEventListener("resize", update);
    };
  }, [ref]);

  return { showLeft, showRight };
}
