/** Deep Research's pre-run question box and its secondary options. */
import { ChevronDown, SlidersHorizontal } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { UploadComposer } from "@/components/build/BuildSurface";
import { ModelLeaderPill } from "@/components/ModelLeaderPill";
import { QueryInput } from "@/components/QueryInput";
import { ScopeControl } from "@/components/ScopeControl";
import { SuggestionChips } from "@/components/SuggestionChips";
import { SourcePicker } from "@/components/SourcePicker";
import { EmptyState } from "@/components/states";
import { cn } from "@/lib/cn";
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
  const [optionsOpen, setOptionsOpen] = useState(false);
  const navigate = useNavigate();
  const changeScope =
    onScopeChange ??
    ((next: ScopeId) => {
      if (next === "standard") navigate("/");
    });
  const recencyLabel =
    r.recencyWindow === "week"
      ? "Past week"
      : r.recencyWindow === "month"
        ? "Past month"
        : "Any time";
  const sourceLabel =
    r.selectedSources.length === 0
      ? "Configured sources"
      : `${r.selectedSources.length} added source${r.selectedSources.length === 1 ? "" : "s"}`;

  return (
    <main className="flex flex-1 flex-col items-center justify-center gap-major px-body pb-[12vh]">
      <EmptyState title="Deep Research" subtitle="Multi-step reports with cited evidence." />
      <div className="flex w-full max-w-measure flex-col gap-inline">
        <div className="flex items-center justify-between gap-inline px-hair">
          <span className="font-ui text-[0.76rem] font-medium text-text-faint">
            Search type
          </span>
          <ScopeControl
            value="deep_research"
            onChange={changeScope}
          />
        </div>
        <QueryInput
          onSubmit={r.submit}
          busy={r.submitting}
          autoFocus
          value={draftValue}
          onValueChange={setDraftValue}
          placeholder="Ask a research question that deserves a multi-page report…"
          showControls={false}
          // Deep Research has no per-run reasoning-effort flag, so there is no
          // Think toggle. Model, scope, recency, sources, and uploads live in
          // the disclosure below rather than crowding this primary row.
          extraControls={
            <>
              <DepthTierSelector value={r.depthTier as Tier} onChange={r.setDepthTier} />
              <button
                type="button"
                aria-expanded={optionsOpen}
                aria-controls="deep-research-options-panel"
                data-disco-control="dr.options"
                onClick={() => setOptionsOpen((open) => !open)}
                className="flex min-h-11 min-w-0 items-center gap-hair rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:border-hairline-strong hover:text-text lg:min-h-0"
              >
                <SlidersHorizontal className="size-3.5 shrink-0 text-text-faint" aria-hidden />
                <span className="shrink-0">Research options</span>
                <span className="hidden truncate text-text-faint sm:inline">
                  · {recencyLabel} · {sourceLabel}
                </span>
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
            optionsOpen ? (
              <div
                id="deep-research-options-panel"
                className="flex flex-col gap-inline border-t border-hairline pt-inline"
              >
                <div className="flex flex-wrap items-center gap-inline">
                  <ModelLeaderPill value={r.leaderId} onChange={r.setLeaderId} />
                  <RecencySelector value={r.recencyWindow} onChange={r.setRecencyWindow} />
                </div>
                <div className="flex flex-wrap items-center justify-between gap-inline">
                  <SourcePicker
                    selected={r.selectedSources}
                    onChange={r.setSelectedSources}
                  />
                  {/* G1/DR-4: opening options mounts the upload affordance. It
                      creates a conversation only after a real file selection. */}
                  <UploadComposer cid={r.preCid} ensureCid={r.ensurePreCid} />
                </div>
              </div>
            ) : null
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
