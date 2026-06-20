import { Square } from "lucide-react";
import { useCallback, useState } from "react";
import { useResearch } from "@/hooks/useResearch";
import { useLastSelectedModel } from "@/hooks/useDriverModels";
import type { ScopeId } from "@/shell/mode";
import { UploadComposer } from "@/components/build/BuildSurface";
import { AnswerDocument } from "./AnswerDocument";
import { ExampleQueries } from "./ExampleQueries";
import { FollowUps } from "./FollowUps";
import { QueryInput } from "./QueryInput";
import { SourcePanel } from "./SourcePanel";
import { TtftIndicator } from "./TtftIndicator";
import { DeepResearchSurface } from "./research/DeepResearchSurface";
import { EmptyState, ErrorState } from "./states";

export function ResearchSurface() {
  const r = useResearch();
  const started = r.scope !== null;
  const noBlocksYet = r.blocks.length === 0 && r.streamingBlockId === null;

  // Per-conversation controls (Prompt 3C): the lead-model override (null = use
  // the Settings default) and the Think flag. Session-local; ride along on submit.
  // R9: `undefined` = UNTOUCHED (seed the display from the sticky last-selected
  // pick); `null` = the user EXPLICITLY chose "Settings default"; a string = an
  // explicit model. The undefined sentinel is what lets a pick control every
  // surface (untouched shows last-selected) WITHOUT trapping the user — clearing
  // back to the default still works (null is respected, not re-hydrated).
  const [leaderId, setLeaderId] = useState<string | null | undefined>(undefined);
  const [scope, setScope] = useState<ScopeId>("standard");
  const [think, setThink] = useState(false);

  const { data: lastSelected } = useLastSelectedModel();
  const effectiveLeaderId = leaderId === undefined ? (lastSelected ?? null) : leaderId;

  const submit = useCallback(
    (query: string) => r.submit(query, { model_override: effectiveLeaderId, think }),
    [r, effectiveLeaderId, think],
  );

  // Scope dispatch: Deep Research has its own surface (own conversation model,
  // own progress + report). The standard scope keeps the single-pass flow
  // unchanged. We dispatch at the surface boundary so the scope selector
  // (visible in the QueryInput cluster on the empty state) gets the user to
  // the right experience without a full mode-slider switch. NOTE: this early
  // return MUST sit after all hook calls — moving it above the useCallback
  // breaks the rules of hooks and remounts the tree blank.
  if (scope === "deep_research") {
    // fix-c #2: forward the leader-pick from the standard scope — without it,
    // switching search→deep-research silently dropped the user-selected model
    // and the deep surface fell back to the default.
    return <DeepResearchSurface onScopeChange={setScope} initialLeaderId={effectiveLeaderId} />;
  }

  const clusterProps = {
    leaderId: effectiveLeaderId,
    onLeaderChange: setLeaderId,
    scope,
    onScopeChange: setScope,
    think,
    onThinkChange: setThink,
  };

  // The shell (Prompt 2) owns the chrome — wordmark in the rail, theme toggle +
  // mode indicator in the top bar — so this surface no longer renders a header;
  // it fills the shell's scrollable main region.
  return (
    <div className="flex min-h-full flex-col pt-section">
      {!started ? (
        <main className="flex flex-1 flex-col items-center justify-center gap-major px-body pb-[12vh]">
          <EmptyState />
          <div className="w-full max-w-measure">
            <QueryInput
              onSubmit={submit}
              busy={r.submitting}
              autoFocus
              {...clusterProps}
              extraControls={
                /* runthru-v2 #9: UploadComposer is now INLINE in the pill row (like
                   the DR surface), not a `footer` block BELOW the cards. Always
                   rendered (UploadComposer self-disables when cid is null) so it
                   doesn't flicker out during the brief pre-create window. */
                <UploadComposer cid={r.preCid} />
              }
            />
            {r.submitError && (
              <p role="alert" className="mt-inline font-ui text-[0.8rem] text-unsupported">
                {r.submitError instanceof Error
                  ? r.submitError.message
                  : "Couldn't start the search — the server didn't respond. Try again."}
              </p>
            )}
          </div>
          <ExampleQueries onPick={submit} />
        </main>
      ) : (
        <main className="flex flex-1 flex-col gap-section pb-major">
          <div className="mx-auto w-full max-w-doc px-body">
            <div className="mx-auto max-w-measure">
              <QueryInput
                onSubmit={submit}
                busy={r.submitting}
                placeholder="Ask a follow-up…"
                {...clusterProps}
              />
            </div>
          </div>

          {/* the question, rendered as a document title (display serif) */}
          <h1 className="mx-auto w-full max-w-doc px-body">
            <span className="mx-auto block max-w-measure font-display text-[1.9rem] font-medium leading-tight tracking-tight text-text">
              {r.scope?.query}
            </span>
          </h1>

          {/* status row — TTFT signature while running, with Stop Generating */}
          {r.phase === "running" && (
            <div className="mx-auto flex w-full max-w-doc items-center justify-between gap-inline px-body">
              <div className="mx-auto flex w-full max-w-measure items-center justify-between">
                <TtftIndicator label={noBlocksYet ? "Searching sources" : "Synthesizing"} />
                <button
                  type="button"
                  onClick={r.stop}
                  className="flex items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:text-text"
                >
                  <Square className="size-3" aria-hidden />
                  Stop generating
                </button>
              </div>
            </div>
          )}

          {/* fixed-height source panel region — pre-allocated so it never shoves
              the answer when sources resolve (CLS guard) */}
          <div className="mx-auto w-full max-w-doc px-body">
            <div className="mx-auto max-w-measure">
              <SourcePanel answer={r.answer} onReScope={r.reScope} />
            </div>
          </div>

          {r.phase === "error" ? (
            <ErrorState message={r.error ?? "Something went wrong."} onRetry={() => r.reScope({})} />
          ) : (
            <AnswerDocument
              blocks={r.blocks}
              partial={r.partial}
              streamingBlockId={r.streamingBlockId}
              answer={r.answer}
              cid={r.runCid}
            />
          )}

          {r.answer && r.phase !== "running" && (
            <FollowUps items={r.answer.follow_ups} onPick={submit} />
          )}
        </main>
      )}
    </div>
  );
}
