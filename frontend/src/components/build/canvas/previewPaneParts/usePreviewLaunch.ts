import { useEffect, useRef, useState } from "react";
import { previewBootstrapUrl, previewErrorReason, type PreviewLaunch } from "@/api/preview";
import { mintOnceForNavigation, type OwnedPreviewMint } from "./helpers";

export interface PreviewLaunchState {
  launch: PreviewLaunch | null;
  launchKey: string;
  launchVersionSeq: number | null;
  minting: boolean;
  launchFailure: string | null;
  frameReady: boolean;
  setFrameReady: (ready: boolean) => void;
}

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
  const mintRef = useRef<OwnedPreviewMint | null>(null);

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

  return { launch, launchKey, launchVersionSeq, minting, launchFailure, frameReady, setFrameReady };
}
