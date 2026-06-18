/**
 * useElementSelect — arm/disarm state + postMessage plumbing for one preview iframe.
 *
 * Call this hook in a component that renders a preview iframe and wants to offer
 * element-selection mode.  Pass the same `iframeRef` to the iframe element's
 * `ref` prop and to `<SelectionOverlay>` for visual feedback.
 *
 * Usage
 * ─────
 *   const iframeRef = useRef<HTMLIFrameElement | null>(null);
 *   const sel = useElementSelect(iframeRef, "null");       // srcdoc → "null"
 *   //                                                     // live-proxy → proxyOrigin
 *   <iframe ref={iframeRef} srcDoc={...} sandbox="allow-scripts" />
 *   <SelectionOverlay armed={sel.armed} selection={sel.selection} ... />
 *
 * @module useElementSelect
 */

import { useState, useEffect, useCallback, useRef } from "react";
import {
  generateNonce,
  parseInFrameMessage,
  sendToFrame,
  makeArmCommand,
  makeDisarmCommand,
  makeWalkUpCommand,
} from "@/lib/selectionBridge";
import type { SelectionEnvelope } from "@/lib/selectionBridge";

export interface ElementSelectState {
  /** Whether selection mode is currently active. */
  armed: boolean;
  /** Arm selection mode: generate a nonce, send arm command to iframe. */
  arm: () => void;
  /** Disarm: send disarm command, clear nonce + selection. */
  disarm: () => void;
  /** Most recently received selection envelope, or null. */
  selection: SelectionEnvelope | null;
  /** Re-target to the selected element's parentElement. */
  walkUp: () => void;
  /** Clear the current selection (without disarming). */
  resetSelection: () => void;
}

/**
 * Manages one preview iframe's selection mode.
 *
 * @param iframeRef     - Ref attached to the `<iframe>` element.
 * @param allowedOrigin - Origin to accept postMessages from.  Pass `"null"`
 *   (the string) for srcdoc iframes (sandboxed without allow-same-origin);
 *   pass the proxy host URL for live-proxy iframes.
 */
export function useElementSelect(
  iframeRef: React.RefObject<HTMLIFrameElement | null>,
  allowedOrigin: string,
): ElementSelectState {
  const [armed, setArmed] = useState(false);
  const [selection, setSelection] = useState<SelectionEnvelope | null>(null);
  // Hold the nonce in a ref so the message listener always sees the latest
  // value without being re-registered on every arm cycle.
  const nonceRef = useRef<string>("");

  const arm = useCallback(() => {
    const nonce = generateNonce();
    nonceRef.current = nonce;
    setArmed(true);
    setSelection(null);
    const iframe = iframeRef.current;
    if (iframe) {
      sendToFrame(iframe, makeArmCommand(nonce));
    }
  }, [iframeRef]);

  const disarm = useCallback(() => {
    const iframe = iframeRef.current;
    if (iframe && nonceRef.current) {
      sendToFrame(iframe, makeDisarmCommand(nonceRef.current));
    }
    nonceRef.current = "";
    setArmed(false);
  }, [iframeRef]);

  const walkUp = useCallback(() => {
    const iframe = iframeRef.current;
    if (armed && iframe && nonceRef.current) {
      sendToFrame(iframe, makeWalkUpCommand(nonceRef.current));
    }
  }, [iframeRef, armed]);

  const resetSelection = useCallback(() => {
    setSelection(null);
  }, []);

  // Listen for messages from the iframe; re-register if allowedOrigin changes.
  useEffect(() => {
    function onMessage(event: MessageEvent) {
      const nonce = nonceRef.current;
      if (!nonce) return; // not armed
      const msg = parseInFrameMessage(event, allowedOrigin, nonce);
      if (!msg) return;
      if (msg.type === "disco:selection") {
        setSelection(msg);
      }
      // disco:hover is handled visually inside the iframe (ring); no host state needed.
    }
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [allowedOrigin]);

  // Disarm on unmount to clean up the in-frame handler.
  useEffect(() => {
    return () => {
      const iframe = iframeRef.current;
      if (iframe && nonceRef.current) {
        sendToFrame(iframe, makeDisarmCommand(nonceRef.current));
      }
    };
  // iframeRef is a stable ref object; we only need the cleanup effect on mount.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return { armed, arm, disarm, selection, walkUp, resetSelection };
}
