import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import type { PreviewLaunch } from "@/api/preview";
import { usePreviewAccess } from "@/hooks/usePreviewAccess";
import { ConfirmDialog } from "./ConfirmDialog";

/** Retain the document and its forms until a confirmed, explicit new launch. */
export function PreviewAccessBoundary({ launch, onReload, onExpiredChange, children }: {
  launch: PreviewLaunch;
  onReload?: () => void | Promise<void>;
  onExpiredChange?: (expired: boolean) => void;
  children: (accept: (expires: unknown, serverTime: unknown) => void) => ReactNode;
}) {
  const { phase, acceptDeadline } = usePreviewAccess(launch.accessDeadline);
  const expired = phase === "expired";
  const container = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const changed = useRef(onExpiredChange);
  changed.current = onExpiredChange;
  const live = useRef(true);
  const inFlight = useRef(false);
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState(false);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  useEffect(() => { changed.current?.(expired); }, [expired]);
  useLayoutEffect(() => {
    if (expired && document.activeElement?.tagName === "IFRAME" &&
      container.current?.contains(document.activeElement)) button.current?.focus();
  }, [expired]);
  async function reload() {
    if (!onReload || inFlight.current) return;
    inFlight.current = true;
    setPending(true);
    setFailure(false);
    try { await onReload(); }
    catch { if (live.current) setFailure(true); }
    finally {
      inFlight.current = false;
      if (live.current) setPending(false);
    }
  }
  return <div ref={container} className="relative flex h-full min-h-0 w-full flex-1 flex-col">
    <div inert={expired} className="h-full min-h-0 w-full flex-1" data-preview-access={phase}>
      {children(acceptDeadline)}
    </div>
    {phase !== "active" && <div
      role={expired || failure ? "alert" : "status"}
      className={`${expired ? "absolute inset-0 flex flex-col items-center justify-center bg-bg/95" : "order-first shrink-0 bg-bg/95"} z-30 border-b border-hairline p-inline font-ui text-sm text-text`}
      data-testid="preview-access-notice"
    >
      <p>{expired ? "Preview access expired. Reload before using this page."
        : phase === "warning" ? "Preview access expires soon. Save your work before reloading."
          : "Preview access expiry is unavailable. Reload if access stops working."}</p>
      {expired && <p className="mt-hair text-text-muted">Your current page has been kept. Nothing was resubmitted.</p>}
      {failure && <p>Preview could not be reloaded. Your current page has been kept.</p>}
      {onReload && <ConfirmDialog
        title="Reload Preview?"
        description="Reloading replaces this page. Unsaved entries may be lost. No previous form submission will be retried."
        confirmLabel="Reload Preview"
        confirmContext="preview-access-reload"
        onConfirm={() => { void reload(); }}
        trigger={<button ref={button} type="button" disabled={pending}
          data-disco-control="preview.access-reload"
          className="mt-inline min-h-11 rounded-control border border-hairline px-body py-hair">
          {pending ? "Reloading Preview…" : "Reload Preview"}
        </button>}
      />}
    </div>}
  </div>;
}
