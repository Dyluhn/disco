import { useEffect, useRef, useState } from "react";
import {
  getLiveBrowserReadiness,
  mintLiveBrowserLaunch,
  startLiveBrowserSession,
  stopLiveBrowserSession,
  touchLiveBrowserSession,
  type PreviewLaunch,
} from "@/api/canvas";
import { useLiveBrowserConfig } from "@/hooks/useModels";

// Live browser (noVNC) state.
// REDESIGN: there is NO manual "Live" button. When the feature is enabled in Settings
// AND the backend can actually stream (live-ready true) AND the agent has a browser
// session up, the live view AUTO-STARTS; it tears down when the session ends, the
// conversation switches, the feature is disabled, or the pane unmounts. Start
// failures remain visible while the screenshot reel provides a bounded fallback.

/** The /browser/live-url `reason`s for which an auto-start is GENUINELY DOOMED — a hard
 * capability/config error that won't fix itself, so we stop retrying for that cid. Every
 * other failure (no_upstream, no_daemon, no_sandbox, network blip) is TRANSIENT: the
 * readiness-gated poll retries it on the next tick once the browser is genuinely up. */
const DOOMED_LIVE_URL_REASONS: ReadonlySet<string> = new Set(["unsupported_backend", "disabled"]);
const LIVE_URL_FAILURE_CAP = 3;

/** Pull the machine `reason` out of a failed getLiveBrowserReadiness/start call
 * (the error message carries the raw JSON body `{reason, message}`). Returns null
 * when there's no parseable reason — treated as TRANSIENT (retryable), the safe
 * default (the poll bounds the retries). */
function liveUrlReason(e: unknown): string | null {
  const msg = e instanceof Error ? e.message : "";
  try {
    const o = JSON.parse(msg) as { reason?: unknown };
    return typeof o?.reason === "string" ? o.reason : null;
  } catch {
    return null;
  }
}

/** A fallback reason string when the server didn't send one — isolated so the
 * auto-start attempt below doesn't carry an extra inline `||` branch. */
function reasonOrDefault(reason: string | undefined, fallback: string): string {
  return reason || fallback;
}

export type LiveStartFailure = "retrying" | "unavailable" | null;

export interface LiveBrowserSession {
  liveView: { launch: PreviewLaunch; ownerCid: string } | null;
  liveReady: boolean;
  iframeConnected: boolean;
  setIframeConnected: (connected: boolean) => void;
  liveStartFailure: LiveStartFailure;
  /** The HONEST "actually streaming" signal: the stack started, the noVNC iframe
   * has loaded, AND the backend still confirms the stack is running. */
  streaming: boolean;
}

/** Encapsulates the entire live-browser (noVNC) session lifecycle for
 * BrowserPane: the readiness poll (which both auto-starts and auto-stops the
 * stack), the disable/conversation-switch teardown, the idle-watchdog
 * heartbeat, and the unmount teardown. BrowserPane only needs the resulting
 * view state to render. */
export function useLiveBrowserSession(cid: string | null): LiveBrowserSession {
  const { data: liveBrowserCfg } = useLiveBrowserConfig();
  const liveBrowserEnabled = liveBrowserCfg?.enabled ?? false;

  // liveView carries the cid that OWNS the stack (`ownerCid`) — this hook is not
  // keyed by cid, so on a conversation switch we must tear down the conversation
  // that opened the view, NOT whatever cid is current now (else we stop the wrong
  // sandbox and leak the old VNC stack).
  const [liveView, setLiveView] = useState<{ launch: PreviewLaunch; ownerCid: string } | null>(
    null,
  );
  // Streamability TRUTH from the side-effect-free /browser/live-ready probe: true only
  // when this backend can actually run + stream the stack (gVisor) AND a sandbox + healthy
  // browser daemon are up. Drives BOTH auto-start (start only when genuinely startable)
  // and auto-stop (when it goes false the session ended → revert to screenshots).
  const [liveReady, setLiveReady] = useState(false);
  // True once the noVNC iframe has actually LOADED — the honest "genuinely streaming"
  // signal gating the green-blink "Live" badge (not merely "we requested a URL").
  const [iframeConnected, setIframeConnected] = useState(false);
  const [liveStartFailure, setLiveStartFailure] = useState<LiveStartFailure>(null);

  // Mirror live state into a ref so the poll/unmount effects read the latest value
  // without re-subscribing on every change.
  const liveViewRef = useRef(liveView);
  liveViewRef.current = liveView;
  // The cid an auto-start is GENUINELY DOOMED for (a hard capability/config error like
  // unsupported_backend) — only THIS suppresses further attempts. A TRANSIENT live-url
  // failure (e.g. no_upstream: live_start succeeded but the port isn't exposed yet) must
  // NOT latch, so the next readiness tick retries once the browser is genuinely up. Reset
  // when the session ends / the conversation switches so a fresh browse can re-start.
  const doomedRef = useRef<string | null>(null);
  const startFailuresRef = useRef(0);
  // Guards against overlapping live-url calls (a slow start spanning >1 poll tick). The
  // poll cadence (4s) is the retry clock, so retries are bounded — never a render-storm.
  const startInFlightRef = useRef(false);

  // Poll the side-effect-free readiness probe while the feature is enabled and there's a
  // cid. The poll is BOTH the auto-start clock AND the auto-stop watchdog:
  //  • ready + no view open → AUTO-START the stack (the same server path the old button
  //    used, no user action). Driving the start from the 4s poll means a transient
  //    live-url failure retries on the NEXT tick — bounded, never a tight render loop.
  //  • not ready while a view is open → the session ended → tear down + revert to
  //    screenshots (2-miss debounce so a single blip doesn't kill a healthy stream).
  // The probe itself has no side-effects (no live_start, no port map).
  useEffect(() => {
    if (!liveBrowserEnabled || !cid) {
      setLiveReady(false);
      setLiveStartFailure(null);
      return;
    }
    let cancelled = false;
    let missStreak = 0;
    doomedRef.current = null; // fresh conversation → clear any prior doomed verdict
    startFailuresRef.current = 0;
    setLiveStartFailure(null);

    // AUTO-START (retry-safe, bounded to this 4s cadence). Only when no view is open,
    // none is in flight, and this cid isn't already known-doomed.
    // A const arrow (not a hoisted function declaration) so TypeScript's closure
    // narrowing carries the `cid` guard above (string | null → string) in here.
    const attemptAutoStart = async (): Promise<void> => {
      if (liveViewRef.current || startInFlightRef.current || doomedRef.current === cid) return;
      startInFlightRef.current = true;
      try {
        const data = await startLiveBrowserSession(cid);
        if (!data.ready || !data.port || !data.novnc_path) {
          throw new Error(JSON.stringify({ reason: reasonOrDefault(data.reason, "no_upstream") }));
        }
        const src = await mintLiveBrowserLaunch(cid, data.port, data.novnc_path);
        if (!src) throw new Error(JSON.stringify({ reason: "no_upstream" }));
        if (cancelled) return;
        startFailuresRef.current = 0;
        setLiveStartFailure(null);
        setIframeConnected(false);
        setLiveView({ launch: src, ownerCid: cid });
      } catch (e: unknown) {
        // Distinguish DOOMED from TRANSIENT without exposing raw backend errors.
        // Transient starts retry only at the 4s readiness cadence and stop after
        // LIVE_URL_FAILURE_CAP attempts; the visible screenshot fallback remains
        // honest instead of silently hammering live-url forever.
        if (cancelled) return;
        startFailuresRef.current += 1;
        const exhausted = startFailuresRef.current >= LIVE_URL_FAILURE_CAP;
        if (DOOMED_LIVE_URL_REASONS.has(liveUrlReason(e) ?? "") || exhausted) {
          doomedRef.current = cid;
          setLiveStartFailure("unavailable");
        } else {
          setLiveStartFailure("retrying");
        }
      } finally {
        startInFlightRef.current = false;
      }
    };

    // Not streamable. If a view of THIS conversation is open, the session has ended —
    // tear down + revert to screenshots. Require 2 consecutive misses (~8s) so a single
    // transient probe blip doesn't kill a healthy stream.
    const endSessionOnMiss = (): void => {
      const lv = liveViewRef.current;
      if (!lv || lv.ownerCid !== cid || missStreak < 2) return;
      stopLiveBrowserSession(lv.ownerCid);
      setLiveView(null);
      setIframeConnected(false);
      doomedRef.current = null; // allow a fresh auto-start if browsing resumes
      startFailuresRef.current = 0;
      setLiveStartFailure(null);
    };

    const probe = async (): Promise<void> => {
      let ready = false;
      try {
        const r = await getLiveBrowserReadiness(cid);
        ready = !!r.ready;
      } catch {
        ready = false;
      }
      if (cancelled) return;
      setLiveReady(ready);
      if (ready) {
        missStreak = 0;
        await attemptAutoStart();
        return;
      }
      missStreak += 1;
      endSessionOnMiss();
    };

    void probe();
    const id = setInterval(() => void probe(), 4000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [liveBrowserEnabled, cid]);

  // Tear the OWNING conversation's stack down when the feature is disabled in Settings,
  // OR when the surface switches to a different conversation while a live view is open
  // (this hook isn't keyed by cid, so we must stop liveView.ownerCid, not `cid`).
  useEffect(() => {
    if (!liveView) return;
    if (!liveBrowserEnabled || liveView.ownerCid !== cid) {
      stopLiveBrowserSession(liveView.ownerCid);
      setLiveView(null);
      setIframeConnected(false);
      doomedRef.current = null;
      startFailuresRef.current = 0;
      setLiveStartFailure(null);
    }
  }, [liveBrowserEnabled, liveView, cid]);

  // Heartbeat: while the live view is open, refresh the sandbox idle watchdog so an
  // ACTIVELY-watched session is never reaped under the user. If the tab is closed /
  // sleeps / loses the network, the heartbeats stop and the watchdog reaps the stack
  // (the backstop for an abandoned view that never fired an unmount/close).
  useEffect(() => {
    if (!liveView) return;
    const owner = liveView.ownerCid;
    const id = setInterval(() => touchLiveBrowserSession(owner), 240_000); // 4 min < the 600s server idle timeout
    return () => clearInterval(id);
  }, [liveView]);

  useEffect(() => {
    return () => {
      // On unmount, stop the live stack this pane started (its owning cid). Reads via the
      // ref so this fires only on unmount (empty deps) with the latest owner.
      if (liveViewRef.current) stopLiveBrowserSession(liveViewRef.current.ownerCid);
    };
  }, []);

  const streaming = !!liveView && iframeConnected && liveReady;

  return { liveView, liveReady, iframeConnected, setIframeConnected, liveStartFailure, streaming };
}
