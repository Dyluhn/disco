import { useCallback, useEffect, useLayoutEffect, useState, type KeyboardEvent, type RefObject } from "react";

interface Options {
  containerRef: RefObject<HTMLDivElement | null>;
  inspectorRef: RefObject<HTMLElement | null>;
  fraction: number;
  onFractionChange: (fraction: number) => void;
  minChat: number;
  minInspector: number;
}

/** Keyboard counterpart to dragging the divider. Read actual rendered pixels,
 * including CSS flex/min-width constraints, rather than trusting saved ratios. */
export function useInspectorKeyboardResize({
  containerRef, inspectorRef, fraction, onFractionChange, minChat, minInspector,
}: Options) {
  const [size, setSize] = useState<{ total: number; width: number } | null>(null);
  const measure = useCallback(() => {
    const total = containerRef.current?.getBoundingClientRect().width ?? 0;
    const width = inspectorRef.current?.getBoundingClientRect().width ?? 0;
    const next = total > 0 && width > 0 ? { total, width } : null;
    setSize((previous) => previous?.total === next?.total && previous?.width === next?.width ? previous : next);
  }, [containerRef, inspectorRef]);

  useLayoutEffect(measure, [fraction, measure]);
  useEffect(() => {
    // Sidebar/layout changes can resize the container without a window resize.
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    if (containerRef.current) observer?.observe(containerRef.current);
    if (inspectorRef.current) observer?.observe(inspectorRef.current);
    window.addEventListener("resize", measure);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, [containerRef, inspectorRef, measure]);

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.defaultPrevented || event.isPropagationStopped() || event.nativeEvent.isComposing
      || event.altKey || event.ctrlKey || event.metaKey) return;
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const total = containerRef.current?.getBoundingClientRect().width ?? 0;
    const width = inspectorRef.current?.getBoundingClientRect().width ?? 0;
    if (total <= 0 || width <= 0) return;
    // Same pixel limits as pointer dragging; left expands the right-hand pane.
    const maximum = Math.max(total - minChat, minInspector);
    const requested = event.key === "Home" ? minInspector
      : event.key === "End" ? maximum
      : width + (event.key === "ArrowLeft" ? 24 : -24);
    event.preventDefault();
    onFractionChange(Math.min(Math.max(requested, minInspector), maximum) / total);
  };

  return {
    onKeyDown,
    valueNow: size ? Math.round(size.width) : undefined,
    valueMin: size ? minInspector : undefined,
    valueMax: size ? Math.round(Math.max(size.total - minChat, minInspector)) : undefined,
    valueText: size ? `${Math.round(size.width)} pixels inspector width` : undefined,
  };
}
