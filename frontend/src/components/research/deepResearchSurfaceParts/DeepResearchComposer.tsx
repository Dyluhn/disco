/** Deep Research's pre-run question box and its secondary options. */
import { ChevronDown, SlidersHorizontal } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { UploadComposer } from "@/components/build/BuildSurface";
import { DriverModelNotice } from "@/components/DriverModelNotice";
import { focusModelControl } from "@/lib/focusModelControl";
import { ModelLeaderPill } from "@/components/ModelLeaderPill";
import { QueryInput } from "@/components/QueryInput";
import { SearchTypeSlider } from "@/components/SearchTypeSlider";
import { SuggestionChips } from "@/components/SuggestionChips";
import { SourcePicker } from "@/components/SourcePicker";
import { EmptyState } from "@/components/states";
import { cn } from "@/lib/cn";
import { DEPTH_TIER_LABEL } from "@/lib/depthTier";
import type { ScopeId } from "@/shell/mode";
import { findModel, useAssignments, useModels } from "@/hooks/useModels";
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
  const [optionsOpen, setOptionsOpen] = useState(false);
  const navigate = useNavigate();
  const changeScope =
    onScopeChange ??
    ((next: ScopeId) => {
      if (next === "standard") navigate("/");
    });
  const depthLabel = DEPTH_TIER_LABEL[r.depthTier as Tier];

  // Same catalogue read as ModelLeaderPill: the explicit pick, else the
  // Settings default — the notice mirrors what would actually lead the run.
  const { data: models } = useModels();
  const { data: assignments } = useAssignments();
  const noticeModel =
    findModel(models, r.leaderId) ?? findModel(models, assignments?.default_model ?? null);

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
          showControls={false}
          // Deep Research has no per-run reasoning-effort flag, so there is no
          // Think toggle. Only the search-type slider and the menu trigger sit
          // in this primary row; depth, model, recency, sources, and uploads
          // all live in the disclosure below.
          extraControls={
            <>
              <SearchTypeSlider value="deep_research" onChange={changeScope} />
              <button
                type="button"
                aria-expanded={optionsOpen}
                aria-controls="deep-research-options-panel"
                data-disco-control="dr.options"
                onClick={() => setOptionsOpen((open) => !open)}
                className="flex min-h-11 min-w-0 items-center gap-hair px-hair py-hair font-ui text-[0.78rem] font-medium text-text-muted transition-colors hover:text-text lg:min-h-0"
              >
                <SlidersHorizontal className="size-3.5 shrink-0 text-text-faint" aria-hidden />
                <span className="shrink-0">Options</span>
                {/* One summary token only: the depth choice hidden in the menu. */}
                <span className="shrink-0 text-text-faint">· {depthLabel}</span>
                <ChevronDown
                  className={cn(
                    "size-3 shrink-0 text-text-faint transition-transform",
                    optionsOpen && "rotate-180",
                  )}
                  aria-hidden
                />
              </button>
            </>
          }
          footer={
            <>
              {optionsOpen && (
                <div
                  id="deep-research-options-panel"
                  className="flex flex-wrap items-center gap-inline border-t border-hairline pt-inline"
                >
                  <DepthTierSelector value={r.depthTier as Tier} onChange={r.setDepthTier} />
                  <ModelLeaderPill value={r.leaderId} onChange={r.setLeaderId} />
                  <RecencySelector value={r.recencyWindow} onChange={r.setRecencyWindow} />
                  <SourcePicker
                    selected={r.selectedSources}
                    onChange={r.setSelectedSources}
                  />
                  {/* G1/DR-4: opening options mounts the upload affordance. It
                      creates a conversation only after a real file selection. */}
                  <UploadComposer cid={r.preCid} ensureCid={r.ensurePreCid} />
                </div>
              )}
              <div className="flex justify-end">
                <DriverModelNotice
                  label={noticeModel?.label ?? null}
                  modelId={noticeModel?.id ?? null}
                  controlId="dr.driver-model"
                  onReveal={() => {
                    setOptionsOpen(true);
                    focusModelControl("deep-research-options-panel");
                  }}
                />
              </div>
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
      {!draftValue.trim() && (
        <SuggestionChips surface="deep_research" onPick={setDraftValue} />
      )}
    </main>
  );
}
