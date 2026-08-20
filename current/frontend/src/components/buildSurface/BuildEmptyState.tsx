/**
 * The Build surface's pre-start landing view: hero + the task composer +
 * suggestion chips. Extracted verbatim from BuildSurface.tsx's `!b.started`
 * branch (PKG-12-FE-BUILD), then re-anchored on the composer: everything
 * optional — model picker, Autonomous toggle, attach/import, and (agent
 * framing) the tools control — lives in the click-to-expand options panel
 * inside the card. Outside the panel the card holds only the textarea, the
 * send button, the "Options" trigger, and the driver-model notice, matching
 * the Search and Deep Research composers' grammar.
 *
 * The Assist tier toggle that used to sit next to Autonomous is deliberately
 * hidden (deprecated control, not removed — see the comment at its old call
 * site below).
 */

import { ChevronDown, SlidersHorizontal, Zap } from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/cn";
import { EmptyState } from "@/components/states";
import { DriverModelNotice } from "@/components/DriverModelNotice";
import { focusModelControl } from "@/lib/focusModelControl";
import { QueryInput } from "@/components/QueryInput";
import { BuildModelPicker } from "@/components/build/BuildModelPicker";
// NOTE: this is `components/build/BuildSurface.tsx` (the OTHER file) — not the
// file this directory decomposes. It is not ours to edit; only its
// `UploadComposer` export is consumed here, unchanged from the original.
import { UploadComposer } from "@/components/build/BuildSurface";
import { ImportProjectDialog } from "@/components/build/ImportProjectDialog";
import { ConnectionsStrip } from "@/components/build/ConnectionsStrip";
import { SuggestionChips } from "@/components/SuggestionChips";
import { useDriverModels, useLastSelectedModel } from "@/hooks/useDriverModels";
import type { BuildFraming } from "@/components/BuildSurface";
import type { BuildController, FramingCopy } from "./types";

export function BuildEmptyState({
  framing,
  copy,
  b,
  draft,
  setDraft,
}: {
  framing: BuildFraming;
  copy: FramingCopy;
  b: BuildController;
  draft: string;
  setDraft: (value: string) => void;
}) {
  const [optionsOpen, setOptionsOpen] = useState(false);
  // The driver-model notice reads the same sources BuildModelPicker resolves
  // its face from: explicit pick → last-selected → agent-server default.
  const { data: driverData } = useDriverModels();
  const { data: lastSelected } = useLastSelectedModel();
  const effectiveModelId = b.modelId ?? lastSelected ?? driverData?.default ?? null;
  const noticeLabel =
    driverData?.models.find((m) => m.id === effectiveModelId)?.label ?? null;

  return (
    <div className="flex min-h-full flex-col pt-section">
      <main className="flex flex-1 flex-col items-center justify-center gap-major px-body pb-[12vh]">
        <EmptyState title={copy.heroTitle} subtitle={copy.heroSubtitle} />
        <div className="w-full max-w-measure">
          <QueryInput
            onSubmit={b.submit}
            busy={b.submitting}
            autoFocus
            placeholder={copy.placeholder}
            value={draft}
            onValueChange={setDraft}
            extraControls={
              <button
                type="button"
                aria-expanded={optionsOpen}
                aria-controls="build-options-panel"
                data-disco-control="build.options"
                onClick={() => setOptionsOpen((open) => !open)}
                className="flex min-h-11 items-center gap-hair px-hair py-hair font-ui text-[0.78rem] font-medium text-text-muted transition-colors hover:text-text lg:min-h-0"
              >
                <SlidersHorizontal className="size-3.5 shrink-0 text-text-faint" aria-hidden />
                <span className="shrink-0">Options</span>
                <ChevronDown
                  className={cn(
                    "size-3 shrink-0 text-text-faint transition-transform",
                    optionsOpen && "rotate-180",
                  )}
                  aria-hidden
                />
              </button>
            }
            footer={
              <>
                {optionsOpen && (
                  <div
                    id="build-options-panel"
                    className="flex flex-wrap items-center gap-inline border-t border-hairline pt-inline"
                  >
                    <BuildModelPicker value={b.modelId} onChange={b.setModelId} surface={framing} />
                    {/* Assist tier toggle deliberately hidden at launch (deprecated
                        control) — b.assistChoice/setAssistChoice still exist and
                        still drive the create/patch payload at its default (off);
                        only the render is gone. See AgentStatusBar.tsx for the
                        matching read-only badge removal. */}
                    {/* Same control grammar as the search/DR menu toggles
                        (ThinkToggle): rounded-control pill, icon + label,
                        accent when on. */}
                    <button
                      type="button"
                      role="switch"
                      aria-checked={b.autonomousChoice}
                      aria-label="Autonomous mode (headless run)"
                      data-disco-control="build.autonomous-toggle"
                      onClick={() => b.setAutonomousChoice(!b.autonomousChoice)}
                      title="Autonomous: the agent runs headless — it won't ask you questions, auto-approves its own plan, and stops cleanly instead of waiting for you. Best for unattended runs; for tricky tasks turn it off so the agent can ask."
                      className={cn(
                        "flex min-h-11 items-center gap-hair rounded-control border px-inline py-hair font-ui text-[0.76rem] transition-colors lg:min-h-0",
                        b.autonomousChoice
                          ? "border-accent/50 bg-surface-1 text-accent"
                          : "border-hairline text-text-muted hover:text-text",
                      )}
                    >
                      <Zap className="size-3.5 shrink-0" aria-hidden />
                      Autonomous
                    </button>
                    {/* G1/DR-4 + W-07: the upload affordance mounts with the menu
                        and self-enables via ensureCid — Attach is usable before a
                        cid exists, lazily creating the build conversation the
                        first message will run. */}
                    <UploadComposer cid={b.preCid} ensureCid={b.ensurePreCid} />
                    {framing === "build" && <ImportProjectDialog />}
                    {/* Agent surface: foreground the MCP tools it can reach (its
                        reason for being). */}
                    {framing === "agent" && <ConnectionsStrip />}
                  </div>
                )}
                <div className="flex justify-end">
                  <DriverModelNotice
                    label={noticeLabel}
                    modelId={effectiveModelId}
                    controlId="build.driver-model"
                    onReveal={() => {
                      setOptionsOpen(true);
                      focusModelControl("build-options-panel");
                    }}
                  />
                </div>
              </>
            }
          />
          {b.submitError && (
            <p role="alert" className="mt-inline text-center font-ui text-[0.8rem] text-unsupported">
              {b.submitError instanceof Error ? b.submitError.message : copy.startError}
            </p>
          )}
        </div>
        <SuggestionChips surface={framing} onPick={setDraft} />
      </main>
    </div>
  );
}
