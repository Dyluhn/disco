/**
 * RP-06 — ShareView: static, self-contained read-only viewer for shared conversation
 * bundles. Fetches the scrubbed JSON bundle via GET /api/share/{token}/bundle and
 * renders the same Activity Feed + Inspector tiers the live Build view shows — but with
 * ZERO WebSocket dependency. Refuses unknown bundle versions with a clear message.
 */

import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { fetchShareBundle, type ShareBundle } from "@/api/agent";
import { StaticRunView } from "@/components/StaticRunView";
import { Loader2, AlertTriangle, Share2 } from "lucide-react";
import type { AgentEvent, ConversationStatus } from "@/types/agent";

type LoadState =
  | { tag: "loading" }
  | { tag: "error"; reason: string }
  | { tag: "done"; bundle: ShareBundle; events: AgentEvent[] };

/** The only version we recognise. Future versions (or a mismatch) get a clear
 *  "this link was made with a newer version" message rather than a cryptic error. */
const SUPPORTED_VERSION = 1;

export function ShareView() {
  const { token } = useParams<{ token: string }>();
  const [state, setState] = useState<LoadState>({ tag: "loading" });

  useEffect(() => {
    if (!token) {
      setState({ tag: "error", reason: "Missing share token in the URL." });
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const bundle = await fetchShareBundle(token);
        if (cancelled) return;
        if (!bundle) {
          setState({
            tag: "error",
            reason: "Share link not found or has been revoked.",
          });
          return;
        }
        if (bundle.bundle_version !== SUPPORTED_VERSION) {
          setState({
            tag: "error",
            reason: `This shared run uses bundle version ${bundle.bundle_version}, but this viewer supports version ${SUPPORTED_VERSION}. The link may have been created with a newer version of the agent.`,
          });
          return;
        }
        // The bundle's events are scrubbed dicts — cast them to AgentEvent[]
        // so the pure selectors accept them. The selectors only read `.kind`,
        // `.source`, `.tool_call`, etc. — all of which survive redaction.
        const events = bundle.events as unknown as AgentEvent[];
        setState({ tag: "done", bundle, events });
      } catch (err) {
        if (cancelled) return;
        setState({
          tag: "error",
          reason:
            err instanceof Error
              ? err.message
              : "Couldn't load the shared run.",
        });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token]);

  // ---- derived state (same pure selectors as the live Build view) ---------

  const status: ConversationStatus = useMemo(() => {
    if (state.tag !== "done") return "IDLE";
    return (
      (state.bundle.state.execution_status as ConversationStatus) ?? "FINISHED"
    );
  }, [state]);

  // ═══════════════════════════════════════════════════════════════════════
  // Loading / error states
  // ═══════════════════════════════════════════════════════════════════════

  if (state.tag === "loading") {
    return (
      <div className="flex min-h-full flex-col items-center justify-center gap-major px-body pb-[12vh]">
        <Loader2 className="size-8 animate-spin text-text-faint" aria-hidden />
        <p className="font-ui text-[0.84rem] text-text-muted">
          Loading shared run…
        </p>
      </div>
    );
  }

  if (state.tag === "error") {
    return (
      <div className="flex min-h-full flex-col items-center justify-center gap-major px-body pb-[12vh]">
        <AlertTriangle
          className="size-8 text-warn"
          aria-label="Error loading shared run"
        />
        <div className="flex max-w-measure flex-col items-center gap-inline text-center">
          <h1 className="font-display text-[1.1rem] font-medium text-text">
            Can't open this shared run
          </h1>
          <p className="font-ui text-[0.82rem] text-text-muted">
            {state.reason}
          </p>
        </div>
      </div>
    );
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Done — render the read-only trace
  // ═══════════════════════════════════════════════════════════════════════

  const { bundle } = state;

  const banner = (
    <div className="border-b border-hairline bg-surface-1 px-body py-inline">
      <div className="flex flex-wrap items-center gap-inline">
        <Share2 className="size-4 text-text-faint" aria-hidden />
        <h1 className="font-display text-[1.1rem] font-medium leading-tight text-text">
          {bundle.title || `Shared run ${bundle.conversation_id.slice(0, 8)}`}
        </h1>
        {bundle.share?.created_at && (
          <span className="font-ui text-[0.7rem] text-text-faint">
            shared {new Date(bundle.share.created_at).toLocaleDateString()}
          </span>
        )}
        <span className="ml-auto shrink-0 rounded-full border border-hairline px-hair font-ui text-[0.62rem] uppercase tracking-wide text-text-faint">
          {bundle.surface ?? "build"}
        </span>
      </div>
    </div>
  );

  // A shared bundle is third-party content viewed in YOUR browser → untrusted.
  return <StaticRunView events={state.events} status={status} banner={banner} untrusted />;
}
