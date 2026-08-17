import { AlertTriangle, Loader2, Plus, X } from "lucide-react";
import type { FormEvent } from "react";
import type { WorkflowAuthoringContext } from "@/types/workflow";
import { errorMessage } from "./draftMapping";
import { McpMountsField } from "./McpMountsField";
import { OutputField } from "./OutputField";
import { ParamsField } from "./ParamsField";
import { PolicyCheckboxes } from "./PolicyCheckboxes";
import { SkillsField } from "./SkillsField";
import { ToolsField } from "./ToolsField";
import type { AuthorDraftFieldsState } from "./useAuthorDraftFields";
import type { AuthorMcpMountsState } from "./useAuthorMcpMounts";
import type { AuthorParamsState } from "./useAuthorParams";
import type { AuthorVerificationState } from "./useAuthorVerification";
import { VerificationField } from "./VerificationField";

interface AdvancedAuthorFormProps {
  hasResult: boolean;
  onClose: () => void;
  contextIsLoading: boolean;
  contextIsError: boolean;
  authoring: WorkflowAuthoringContext | undefined;
  draftFields: AuthorDraftFieldsState;
  paramsField: AuthorParamsState;
  mcpField: AuthorMcpMountsState;
  verification: AuthorVerificationState;
  writableMcpNeedsPolicy: boolean;
  authorIsError: boolean;
  authorError: unknown;
  authorIsPending: boolean;
  canSubmit: boolean;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
}

export function AdvancedAuthorForm({
  hasResult,
  onClose,
  contextIsLoading,
  contextIsError,
  authoring,
  draftFields,
  paramsField,
  mcpField,
  verification,
  writableMcpNeedsPolicy,
  authorIsError,
  authorError,
  authorIsPending,
  canSubmit,
  onSubmit,
}: AdvancedAuthorFormProps) {
  return (
    <section className="flex flex-col gap-inline border-t border-hairline pt-section">
      <div className="flex items-center justify-between gap-inline">
        <h3 className="font-ui text-[0.92rem] font-semibold text-text">Edit details (Advanced)</h3>
        {hasResult && (
          <button
            type="button"
            onClick={onClose}
            className="inline-flex w-fit items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted hover:text-text"
          >
            <X className="size-3.5" aria-hidden />
            Close
          </button>
        )}
      </div>

      {contextIsLoading && (
        <div className="flex items-center gap-hair font-ui text-[0.86rem] text-text-muted">
          <Loader2 className="size-4 animate-spin" aria-hidden />
          Loading authoring context.
        </div>
      )}

      {(contextIsError || (!contextIsLoading && !authoring)) && (
        <div
          role="alert"
          className="rounded-control border border-hairline border-l-2 border-l-warn bg-surface-2 p-inline font-ui text-[0.86rem] text-text"
        >
          Could not load workflow authoring context.
        </div>
      )}

      {authoring && (
        <form className="flex flex-col gap-section" onSubmit={onSubmit}>
          <div className="grid gap-inline md:grid-cols-[minmax(0,18rem)_1fr]">
            <label className="flex flex-col gap-hair font-ui text-[0.8rem] text-text-muted">
              Name
              <input
                aria-label="Name"
                value={draftFields.name}
                onChange={(event) => draftFields.setName(event.target.value)}
                className="rounded-control border border-hairline bg-surface-2 px-inline py-hair text-[0.84rem] text-text outline-none focus:border-accent"
              />
            </label>
            <label className="flex flex-col gap-hair font-ui text-[0.8rem] text-text-muted">
              Card
              <textarea
                aria-label="Card"
                value={draftFields.card}
                onChange={(event) => draftFields.setCard(event.target.value)}
                rows={3}
                className="rounded-control border border-hairline bg-surface-2 px-inline py-hair text-[0.84rem] text-text outline-none focus:border-accent"
              />
              <span className={draftFields.cardHasBlankLine ? "text-unsupported" : "text-text-faint"}>
                One paragraph only; blank lines are rejected.
              </span>
            </label>
          </div>

          <ToolsField
            builtinTools={authoring.builtin_tools}
            selectedTools={draftFields.tools}
            onChange={draftFields.setTools}
          />

          <ParamsField
            params={paramsField.params}
            paramTypes={authoring.param_types}
            duplicateParams={paramsField.duplicateParams}
            invalidParam={paramsField.invalidParam}
            onAdd={paramsField.addParam}
            onUpdate={paramsField.updateParam}
            onRemove={paramsField.removeParam}
          />

          <McpMountsField
            mcpServers={authoring.mcp_servers}
            mcpMounts={mcpField.mcpMounts}
            emptyMcpMount={mcpField.emptyMcpMount}
            writableMcpNeedsPolicy={writableMcpNeedsPolicy}
            onAdd={mcpField.addMcpMount}
            onUpdateServer={mcpField.updateMountServer}
            onUpdateReadOnly={mcpField.updateMountReadOnly}
            onToggleTool={mcpField.toggleMountTool}
            onRemove={mcpField.removeMount}
          />

          <SkillsField
            skills={authoring.skills}
            selectedSkills={draftFields.skills}
            onChange={draftFields.setSkills}
          />

          <OutputField
            outputPathTemplate={draftFields.outputPathTemplate}
            onOutputPathTemplateChange={draftFields.setOutputPathTemplate}
            outputFormat={draftFields.outputFormat}
            onOutputFormatChange={draftFields.setOutputFormat}
            outputFormats={authoring.output_formats}
          />

          <VerificationField
            verifyChecks={verification.verifyChecks}
            verifyDraft={verification.verifyDraft}
            onVerifyDraftChange={verification.setVerifyDraft}
            onVerifyKeyDown={verification.handleVerifyKeyDown}
            onAddVerifyChecks={verification.addVerifyChecks}
            onRemoveVerifyCheck={verification.removeVerifyCheck}
            finalizer={draftFields.finalizer}
            onFinalizerChange={draftFields.setFinalizer}
          />

          <PolicyCheckboxes
            allowsWrites={draftFields.allowsWrites}
            onAllowsWritesChange={draftFields.setAllowsWrites}
            untrustedContent={draftFields.untrustedContent}
            onUntrustedContentChange={draftFields.setUntrustedContent}
          />

          {authorIsError && (
            <div
              role="alert"
              className="flex items-start gap-hair rounded-control border border-unsupported/50 bg-surface-2 p-inline font-ui text-[0.82rem] text-unsupported"
            >
              <AlertTriangle className="size-4 shrink-0" aria-hidden />
              {errorMessage(authorError, "Workflow authoring failed.")}
            </div>
          )}

          <button
            type="submit"
            disabled={!canSubmit}
            className="inline-flex w-fit items-center justify-center gap-hair rounded-control border border-hairline px-body py-hair font-ui text-[0.84rem] text-text-muted transition-colors hover:border-accent/60 hover:text-text disabled:cursor-not-allowed disabled:opacity-50"
          >
            {authorIsPending ? (
              <Loader2 className="size-3.5 animate-spin" aria-hidden />
            ) : (
              <Plus className="size-3.5" aria-hidden />
            )}
            Create draft
          </button>
        </form>
      )}
    </section>
  );
}
