import { useCallback, useEffect, useRef, useState } from "react";
import { previewBootstrapUrl, previewErrorReason, type PreviewLaunch } from "@/api/preview";
import { mintOnceForNavigation, type OwnedPreviewMint } from "./helpers";

export interface PreviewLaunchState {
  launch: PreviewLaunch | null;
  launchKey: string;
  launchVersionSeq: number | null;
  minting: boolean;
  launchFailure: string | null;
  /** Reason code for a launch that MINTED FINE but whose frame never handed
   * back its bootstrap handshake. Separate from `launchFailure` on purpose —
   * see `computeVisibleFailure`. */
  bootstrapFailure: string | null;
  /** Hand to `PreviewLaunchFrame` as `onBootstrapReady` to disarm the watchdog. */
  onBootstrapReady: () => void;
  frameReady: boolean;
  setFrameReady: (ready: boolean) => void;
}

/** How long to wait for the POST trampoline's `disco-preview-bootstrap-ready`
 * message before declaring the preview origin unreachable (UI-44).
 *
 * Deliberately generous. The handshake covers a real network round trip to a
 * freshly started preview container, and a false positive here throws away a
 * preview that was merely slow. 15s is far past any healthy handshake — the
 * trampoline posts as soon as it parses — and far short of how long a user
 * will sit in front of a raw proxy error page under a "Preparing…" strip that
 * never resolves, which is the failure this replaces. */
export const PREVIEW_BOOTSTRAP_TIMEOUT_MS = 15_000;

/** Owns the one-shot capability mint for the current navigation target: the
 * `launch`/`launchKey`/`launchVersionSeq`/`minting`/`launchFailure`/`frameReady`
 * state plus the owned-mint ref that keys the in-flight promise so a re-render
 * with the same navigation target reuses it instead of minting twice. */
export function usePreviewLaunch(
  cid: string | null,
  ownsCanonicalPreview: boolean,
  requestedLaunchKey: string,
  selectedTarget: string,
  selectedVersionSeq: number | null,
): PreviewLaunchState {
  const [launch, setLaunch] = useState<PreviewLaunch | null>(null);
  const [launchKey, setLaunchKey] = useState("");
  const [launchVersionSeq, setLaunchVersionSeq] = useState<number | null>(null);
  const [minting, setMinting] = useState(false);
  const [launchFailure, setLaunchFailure] = useState<string | null>(null);
  const [frameReady, setFrameReady] = useState(false);
  const [bootstrapFailure, setBootstrapFailure] = useState<string | null>(null);
  const mintRef = useRef<OwnedPreviewMint | null>(null);
  const bootstrapTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const onBootstrapReady = useCallback(() => {
    if (bootstrapTimerRef.current !== null) clearTimeout(bootstrapTimerRef.current);
    bootstrapTimerRef.current = null;
  }, []);

  // The bootstrap watchdog. An intent launch is driven by a POST trampoline
  // whose `disco-preview-bootstrap-ready` message is the ONLY thing that lets
  // the frame's load reach us. When the preview host answers the trampoline's
  // own request with an error page (nginx 403, "preview origin expired"), that
  // message never arrives, the mint has already succeeded so nothing records a
  // failure, and the pane sits on the raw error page under a "Preparing Preview
  // update…" strip forever (UI-44). A launch with no `intent` is a plain `src`
  // navigation with no handshake, so it must never arm this.
  useEffect(() => {
    setBootstrapFailure(null);
    if (!launch?.intent) return;
    const timer = setTimeout(
      () => setBootstrapFailure("preview_bootstrap_timeout"),
      PREVIEW_BOOTSTRAP_TIMEOUT_MS,
    );
    bootstrapTimerRef.current = timer;
    return () => {
      clearTimeout(timer);
      if (bootstrapTimerRef.current === timer) bootstrapTimerRef.current = null;
    };
  }, [launch, launchKey]);

  useEffect(() => {
    let cancelled = false;
    if (!cid || !ownsCanonicalPreview) {
      mintRef.current = null;
      return;
    }
    setMinting(true);
    setLaunchFailure(null);
    void mintOnceForNavigation(mintRef, requestedLaunchKey, () =>
      previewBootstrapUrl(cid, selectedTarget, selectedVersionSeq),
    )
      .then((nextLaunch) => {
        if (cancelled) return;
        if (nextLaunch === null) {
          setLaunchFailure("isolated preview access could not be established");
          return;
        }
        setLaunch(nextLaunch);
        setLaunchKey(requestedLaunchKey);
        setLaunchVersionSeq(selectedVersionSeq);
        setFrameReady(false);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        const reason = error instanceof Error ? previewErrorReason(error.message) : null;
        setLaunchFailure(reason ?? "isolated preview access could not be established");
      })
      .finally(() => {
        if (!cancelled) setMinting(false);
      });
    return () => {
      cancelled = true;
    };
  }, [cid, ownsCanonicalPreview, requestedLaunchKey, selectedTarget, selectedVersionSeq]);

  return {
    launch,
    launchKey,
    launchVersionSeq,
    minting,
    launchFailure,
    bootstrapFailure,
    onBootstrapReady,
    frameReady,
    setFrameReady,
  };
}
