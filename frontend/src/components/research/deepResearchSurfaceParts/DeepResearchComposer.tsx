/**
 * DeepResearchComposer — the pre-run composer cluster (empty state): QueryInput
 * + SuggestionChips + SourcePicker + DepthTierSelector + RecencySelector.
 * Relocated verbatim from DeepResearchSurface.tsx's `!started` branch. See
 * that file's header for the surface map.
 */
import { UploadComposer } from "@/components/build/BuildSurface";
import { QueryInput } from "@/components/QueryInput";
import { SuggestionChips } from "@/components/SuggestionChips";
import { SourcePicker } from "@/components/SourcePicker";
import { EmptyState } from "@/components/states";
import type { ScopeId } from "@/shell/mode";
import type { useDeepResearch } from "@/hooks/useDeepResearch";
import { DepthTierSelector, type Tier } from "../DepthTierSelector";
import { RecencySelector } from "../RecencySelector";

interface Props {
  r: ReturnType<typeof useDeepResearch>;
  draftValue: string;
  setDraftValue: (next: string) => void;
  onScopeChange?: (next: ScopeId) => void;
}

export function DeepResearchComposer({ r, draftValue, setDraftValue, onScopeChange }: Props) {
  return (
    <main className="flex flex-1 flex-col items-center justify-center gap-major px-body pb-[12vh]">
      <EmptyState title="Deep Research" subtitle="Multi-step reports with cited evidence." />
      <div className="flex w-full max-w-measure flex-col gap-inline">
        <QueryInput
          onSubmit={r.submit}
          busy={r.submitting}
          autoFocus
          value={draftValue}
          onValueChange={setDraftValue}
          placeholder="Ask a research question that deserves a multi-page report…"
          leaderId={r.leaderId}
          onLeaderChange={r.setLeaderId}
          scope={"deep_research" as ScopeId}
          onScopeChange={onScopeChange ?? (() => {})}
          // BW-04: Deep Research does not honor a per-run reasoning-effort
          // flag (its create frame carries depth/recency/iterative, not
          // `think`), so the Think toggle is OMITTED rather than rendered
          // inert (always-off, no-op) — no false affordance. Omitting
          // onThinkChange drops only the toggle; model/scope still render.
          extraControls={
            // R10: Depth/Recency/Iterative now sit INLINE in the same pill row
            // as the model/scope cluster (via QueryInput's `extraControls`),
            // not a full-width footer block that grew the card and reflowed the
            // centered layout. The hint paragraph is dropped (it was layout bulk;
            // the leader pill already names the driver model).
            <>
              <DepthTierSelector value={r.depthTier as Tier} onChange={r.setDepthTier} />
              <RecencySelector value={r.recencyWindow} onChange={r.setRecencyWindow} />
              {/* IterativeToggle removed 2026-07-07 — backend stub stays default-off */}
              <SourcePicker
                selected={r.selectedSources}
                onChange={r.setSelectedSources}
              />
              {/* G1/DR-4 + runthru-v2 #9: UploadComposer always rendered (it
                  self-disables when cid is null) so the attach affordance does
                  NOT vanish during the brief preCid re-create window on a
                  settings change — it just dims until the new cid resolves. */}
              <UploadComposer cid={r.preCid} ensureCid={r.ensurePreCid} />
            </>
          }
        />
        {r.submitError && (
          <p role="alert" className="font-ui text-[0.8rem] text-unsupported">
            {r.submitError instanceof Error
              ? r.submitError.message
              : "Couldn't start deep research — the server didn't respond. Try again."}
          </p>
        )}
      </div>
      <SuggestionChips surface="deep_research" onPick={setDraftValue} />
    </main>
  );
}
