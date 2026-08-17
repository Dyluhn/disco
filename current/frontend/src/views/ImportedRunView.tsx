/**
 * rp-06 residue — ImportedRunView: the read-only view for a conversation IMPORTED
 * from a share bundle (/imported/:cid). Fetches the already-complete event log +
 * status and renders it via the shared StaticRunView (untrusted → hardened preview).
 * The conversation is read-only AT THE SERVER (every kick path 409s); this view just
 * never offers a compose/resume affordance and flags its provenance.
 */

import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { AlertTriangle, Download, Loader2 } from "lucide-react";
import { fetchConversationRun } from "@/api/agent";
import { StaticRunView } from "@/components/StaticRunView";
import type { AgentEvent, ConversationStatus } from "@/types/agent";

type LoadState =
  | { tag: "loading" }
  | { tag: "error"; reason: string }
  | { tag: "done"; events: AgentEvent[]; status: ConversationStatus };

export function ImportedRunView() {
  const { cid } = useParams<{ cid: string }>();
  const [state, setState] = useState<LoadState>({ tag: "loading" });

  useEffect(() => {
    if (!cid) {
      setState({ tag: "error", reason: "Missing conversation id in the URL." });
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const run = await fetchConversationRun(cid);
        if (cancelled) return;
        setState({
          tag: "done",
          events: run.events as unknown as AgentEvent[],
          status: run.status as ConversationStatus,
        });
      } catch (err) {
        if (cancelled) return;
        setState({
          tag: "error",
          reason: err instanceof Error ? err.message : "Couldn't load the imported run.",
        });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [cid]);

  if (state.tag === "loading") {
    return (
      <div className="flex min-h-full flex-col items-center justify-center gap-major px-body pb-[12vh]">
        <Loader2 className="size-8 animate-spin text-text-faint" aria-hidden />
        <p className="font-ui text-[0.84rem] text-text-muted">Loading imported run…</p>
      </div>
    );
  }

  if (state.tag === "error") {
    return (
      <div className="flex min-h-full flex-col items-center justify-center gap-major px-body pb-[12vh]">
        <AlertTriangle className="size-8 text-warn" aria-label="Error loading imported run" />
        <div className="flex max-w-measure flex-col items-center gap-inline text-center">
          <h1 className="font-display text-[1.1rem] font-medium text-text">
            Can't open this imported run
          </h1>
          <p className="font-ui text-[0.82rem] text-text-muted">{state.reason}</p>
        </div>
      </div>
    );
  }

  const banner = (
    <div className="border-b border-hairline bg-surface-1 px-body py-inline" data-imported-cid={cid}>
      <div className="flex flex-wrap items-center gap-inline">
        <Download className="size-4 text-text-faint" aria-hidden />
        <h1 className="font-display text-[1.1rem] font-medium leading-tight text-text">
          Imported run
        </h1>
        <span className="rounded-full border border-warn/40 px-hair font-ui text-[0.62rem] uppercase tracking-wide text-warn">
          imported · read-only
        </span>
        <span className="font-ui text-[0.7rem] text-text-faint">
          A run imported from a share bundle — it cannot be continued or re-run.
        </span>
      </div>
    </div>
  );

  return <StaticRunView events={state.events} status={state.status} banner={banner} untrusted />;
}
