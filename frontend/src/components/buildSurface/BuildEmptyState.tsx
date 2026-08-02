/**
 * The Build surface's pre-start landing view: hero + model picker + assist/
 * autonomous toggles + the task composer + suggestion chips. Extracted verbatim
 * from BuildSurface.tsx's `!b.started` branch (PKG-12-FE-BUILD).
 */

import { cn } from "@/lib/cn";
import { EmptyState } from "@/components/states";
import { QueryInput } from "@/components/QueryInput";
import { BuildModelPicker } from "@/components/build/BuildModelPicker";
// NOTE: this is `components/build/BuildSurface.tsx` (the OTHER file) — not the
// file this directory decomposes. It is not ours to edit; only its
// `UploadComposer` export is consumed here, unchanged from the original.
import { UploadComposer } from "@/components/build/BuildSurface";
import { ImportProjectDialog } from "@/components/build/ImportProjectDialog";
import { ConnectionsStrip } from "@/components/build/ConnectionsStrip";
import { SuggestionChips } from "@/components/SuggestionChips";
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
  return (
    <div className="flex min-h-full flex-col pt-section">
      <main className="flex flex-1 flex-col items-center justify-center gap-major px-body pb-[12vh]">
        <EmptyState title={copy.heroTitle} subtitle={copy.heroSubtitle} />
        <div className="w-full max-w-measure">
          <div className="mb-inline flex items-center justify-between gap-inline">
            <BuildModelPicker value={b.modelId} onChange={b.setModelId} />
            <div className="flex items-center gap-hair">
              <button
                type="button"
                role="switch"
                aria-checked={b.assistChoice}
                aria-label="Assist tier (weak-model compensations)"
                data-disco-control="build.assist-toggle"
                onClick={() => b.setAssistChoice(!b.assistChoice)}
                title="Assist: weak-model compensations (file-state reinforcement, simplified tool surface + plan handling). Turn ON for a genuinely small/weak model. Leave OFF for capable models — it is NEVER auto-enabled, even for local models like Qwen 27B."
                className={cn(
                  "flex items-center gap-hair rounded-full border px-inline py-px font-ui text-[0.72rem] transition-colors",
                  b.assistChoice
                    ? "border-accent/50 bg-accent/5 text-accent"
                    : "border-hairline text-text-faint hover:text-text-muted",
                )}
              >
                {b.assistChoice ? "assist: on" : "assist: off"}
              </button>
              <button
                type="button"
                role="switch"
                aria-checked={b.autonomousChoice}
                aria-label="Autonomous mode (headless run)"
                data-disco-control="build.autonomous-toggle"
                onClick={() => b.setAutonomousChoice(!b.autonomousChoice)}
                title="Autonomous: the agent runs headless — it won't ask you questions, auto-approves its own plan, and stops cleanly instead of waiting for you. Best for unattended runs; for tricky tasks leave it off so the agent can ask."
                className={cn(
                  "flex items-center gap-hair rounded-full border px-inline py-px font-ui text-[0.72rem] transition-colors",
                  b.autonomousChoice
                    ? "border-accent/50 bg-accent/5 text-accent"
                    : "border-hairline text-text-faint hover:text-text-muted",
                )}
              >
                {b.autonomousChoice ? "autonomous: on" : "autonomous: off"}
              </button>
            </div>
          </div>
          <QueryInput
            onSubmit={b.submit}
            busy={b.submitting}
            autoFocus
            placeholder={copy.placeholder}
            value={draft}
            onValueChange={setDraft}
            footer={
              /* G1/DR-4 + W-07: UploadComposer in the empty state. Always
                 rendered (no longer gated on preCid, which HID the attach while
                 the eager mount-create was in flight). It self-enables via
                 ensureCid — Attach is usable before a cid exists, lazily
                 creating the build conversation the first message will run. */
              <div className="flex items-center gap-inline">
                <UploadComposer cid={b.preCid} ensureCid={b.ensurePreCid} />
                {framing === "build" && <ImportProjectDialog />}
              </div>
            }
          />
          <p className="mt-inline text-center font-ui text-[0.78rem] text-text-faint">
            The agent works in a sandbox and shows its plan.{" "}
            {b.autonomousChoice
              ? "It runs headless — risky steps auto-approve and it won't stop to ask."
              : "Risky steps pause for your approval."}
          </p>
          {/* Agent surface: foreground the MCP tools it can reach (its reason for being). */}
          {framing === "agent" && <ConnectionsStrip />}
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
