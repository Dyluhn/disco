import { cn } from "@/lib/cn";

/**
 * Shared mobile primitive: pair with any `overflow-x-auto` strip (tabs,
 * chips, pills) to give it a visible "this scrolls" affordance instead of a
 * silent hard truncation at the container edge.
 *
 * USAGE — three steps on your existing scroll container, no markup wrapper
 * needed:
 *   1. `const scrollRef = useRef<HTMLElement>(null);`
 *   2. `const { showLeft, showRight } = useScrollFade(scrollRef);` (from
 *      `@/hooks/useScrollFade`)
 *   3. Add `ref={scrollRef}` and `relative` to the scrolling element's own
 *      className, then render `<ScrollFadeEdges showLeft={showLeft}
 *      showRight={showRight} />` as its LAST children (it renders two
 *      absolutely-positioned gradient slivers, so the scroll container needs
 *      `relative` to anchor them — `sticky` containers already qualify).
 *
 * The edges fade in/out based on real scroll position (via a scroll listener
 * + ResizeObserver), so a strip that already fits on screen shows no fade at
 * all, and a strip you've scrolled to the end of only fades the side that
 * still has hidden content.
 *
 * Gate the whole thing behind `lg:hidden` (or whatever breakpoint your
 * container stops scrolling at) at the call site if desktop uses a
 * non-scrolling layout — ScrollFadeEdges itself doesn't assume a breakpoint.
 */
export function ScrollFadeEdges({
  showLeft,
  showRight,
  className,
}: {
  showLeft: boolean;
  showRight: boolean;
  className?: string;
}) {
  return (
    <>
      <span
        aria-hidden
        className={cn(
          // Tailwind v4 renamed the gradient utilities: it's `bg-linear-to-*`,
          // NOT the v3-era `bg-gradient-to-*` (which silently emits no CSS at
          // all under v4 — an easy, invisible way to ship a fade that never
          // fades). Enforced mechanically by the no-restricted-syntax rule in
          // eslint.config.js, which fails the lint on any v3 gradient class.
          "pointer-events-none absolute inset-y-0 left-0 z-10 w-8 bg-linear-to-r from-bg to-transparent transition-opacity duration-150",
          showLeft ? "opacity-100" : "opacity-0",
          className,
        )}
      />
      <span
        aria-hidden
        className={cn(
          "pointer-events-none absolute inset-y-0 right-0 z-10 w-8 bg-linear-to-l from-bg to-transparent transition-opacity duration-150",
          showRight ? "opacity-100" : "opacity-0",
          className,
        )}
      />
    </>
  );
}
