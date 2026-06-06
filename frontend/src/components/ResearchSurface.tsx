import { Square } from "lucide-react";
import { useCallback, useState } from "react";
import { useResearch } from "@/hooks/useResearch";
import type { ScopeId } from "@/shell/mode";
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
  const [leaderId, setLeaderId] = useState<string | null>(null);
  const [scope, setScope] = useState<ScopeId>("standard");
  const [think, setThink] = useState(false);

  const submit = useCallback(
    (query: string) => r.submit(query, { model_override: leaderId, think }),
    [r, leaderId, think],
  );

  // Scope dispatch: Deep Research has its own surface (own conversation model,
  // own progress + report). The standard scope keeps the single-pass flow
  // unchanged. We dispatch at the surface boundary so the scope selector
  // (visible in the QueryInput cluster on the empty state) gets the user to
  // the right experience without a full mode-slider switch. NOTE: this early
  // return MUST sit after all hook calls — moving it above the useCallback
  // breaks the rules of hooks and remounts the tree blank.
  if (scope === "deep_research") {
    return <DeepResearchSurface onScopeChange={setScope} />;
  }

  const clusterProps = {
    leaderId,
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
            <QueryInput onSubmit={submit} busy={r.submitting} autoFocus {...clusterProps} />
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
