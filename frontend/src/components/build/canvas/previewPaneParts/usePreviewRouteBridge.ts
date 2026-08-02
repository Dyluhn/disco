import { useLayoutEffect, type MutableRefObject } from "react";
import type { PreviewLaunch } from "@/api/preview";
import { originOf } from "./helpers";

/** The injected host bridge reports the page's current route. Refreshes and
 * static-server file updates then remint that route instead of jumping to `/`.
 * `currentRouteRef` is owned here (this is the only writer); it is threaded in
 * from the caller because other coordinator logic (the rollback flow) resets
 * it too, so it has to live above both. */
export function usePreviewRouteBridge(
  launch: PreviewLaunch | null,
  frameRef: MutableRefObject<HTMLIFrameElement | null>,
  currentRouteRef: MutableRefObject<string>,
): void {
  useLayoutEffect(() => {
    const allowedOrigin = originOf(launch?.url ?? null);
    function onMessage(event: MessageEvent) {
      if (
        !allowedOrigin ||
        event.origin !== allowedOrigin ||
        event.source !== frameRef.current?.contentWindow ||
        !event.data ||
        typeof event.data !== "object"
      ) {
        return;
      }
      const message = event.data as Record<string, unknown>;
      if (
        message.channel === "disco-preview" &&
        message.type === "location" &&
        typeof message.path === "string" &&
        message.path.startsWith("/") &&
        message.path.length <= 4096
      ) {
        currentRouteRef.current = message.path;
      }
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [launch, frameRef, currentRouteRef]);
}
