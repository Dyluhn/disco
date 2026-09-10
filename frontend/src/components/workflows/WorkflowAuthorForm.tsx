import type { FormEvent } from "react";
import {
  useApproveWorkflow,
  useAuthoringContext,
  useAuthorWorkflow,
  useDraftFromDescription,
} from "@/hooks/useWorkflows";
import type { WorkflowAuthorInput } from "@/types/workflow";
import { AdvancedAuthorForm } from "./authorForm/AdvancedAuthorForm";
import { authorInputFromReview, resultFromAuthorDraft } from "./authorForm/draftMapping";
import { DescribeStep } from "./authorForm/DescribeStep";
import { DraftReviewPanel } from "./authorForm/DraftReviewPanel";
import {
  canDraftAuthorForm,
  canSubmitAuthorForm,
  mcpWritesNeedPolicy,
} from "./authorForm/formValidation";
import { useAuthorDraftFields } from "./authorForm/useAuthorDraftFields";
import { useAuthorMcpMounts } from "./authorForm/useAuthorMcpMounts";
import { useAuthorParams } from "./authorForm/useAuthorParams";
import { useAuthorVerification } from "./authorForm/useAuthorVerification";

export function WorkflowAuthorForm() {
  const context = useAuthoringContext();
  const author = useAuthorWorkflow();
  const draft = useDraftFromDescription();
  const approve = useApproveWorkflow();

  const draftFields = useAuthorDraftFields();
  const paramsField = useAuthorParams();
  const mcpField = useAuthorMcpMounts();
  const verification = useAuthorVerification();

  const authoring = context.data;
  const writableMcpNeedsPolicy = mcpWritesNeedPolicy(
    mcpField.hasWritableMount,
    draftFields.allowsWrites,
  );
  const canDraft = canDraftAuthorForm(draftFields.description, draft.isPending);
  const canSubmit = canSubmitAuthorForm({
    hasAuthoringContext: Boolean(authoring),
    name: draftFields.name,
    card: draftFields.card,
    outputPathTemplate: draftFields.outputPathTemplate,
    cardHasBlankLine: draftFields.cardHasBlankLine,
    duplicateParams: paramsField.duplicateParams,
    invalidParam: paramsField.invalidParam,
    writableMcpNeedsPolicy,
    emptyMcpMount: mcpField.emptyMcpMount,
    isSubmitting: author.isPending,
  });

  function applyAuthorInput(input: WorkflowAuthorInput) {
    draftFields.setName(input.name);
    draftFields.setCard(input.card);
    draftFields.setTools(input.tools);
    paramsField.setParams(input.params);
    mcpField.setMcpMounts(input.mcp_mounts);
    draftFields.setSkills(input.skills);
    draftFields.setAllowsWrites(input.allows_writes);
    draftFields.setUntrustedContent(input.untrusted_content);
    draftFields.setOutputPathTemplate(input.output_path_template);
    draftFields.setOutputFormat(input.output_format);
    verification.setVerifyChecks(input.verify_checks);
    verification.setVerifyDraft("");
    draftFields.setFinalizer(input.finalizer ?? "");
  }

  async function handleDescribeSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canDraft) return;
    const cleaned = draftFields.description.trim();
    const response = await draft.mutateAsync(cleaned);
    draftFields.setResult(response);
    applyAuthorInput(authorInputFromReview(response.workflow));
    draftFields.setAdvancedOpen(false);
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSubmit) return;
    const input: WorkflowAuthorInput = {
      name: draftFields.name.trim(),
      card: draftFields.card.trim(),
      params: paramsField.params.map((param) => ({
        name: param.name.trim(),
        type: param.type,
        required: param.required,
        description: param.description?.trim() || undefined,
      })),
      tools: draftFields.tools,
      mcp_mounts: mcpField.mcpMounts,
      skills: draftFields.skills,
      allows_writes: draftFields.allowsWrites,
      untrusted_content: draftFields.untrustedContent,
      output_path_template: draftFields.outputPathTemplate.trim(),
      output_format: draftFields.outputFormat,
      verify_checks: verification.verifyChecks,
      finalizer: draftFields.finalizer.trim() || null,
    };
    const response = await author.mutateAsync(input);
    draftFields.setResult(resultFromAuthorDraft(response, draftFields.description.trim()));
    draftFields.setAdvancedOpen(false);
  }

  async function handleApprove() {
    if (!draftFields.result) return;
    const approved = await approve.mutateAsync({
      instanceId: draftFields.result.workflow.instance_id,
      surfaceShownDigest: draftFields.result.workflow.surface_shown_digest,
    });
    draftFields.setResult({ ...draftFields.result, workflow: approved });
  }

  function handleEditDetails() {
    if (draftFields.result) applyAuthorInput(authorInputFromReview(draftFields.result.workflow));
    draftFields.setAdvancedOpen(true);
  }

  function handleStartOver() {
    draftFields.setDescription("");
    draftFields.setResult(null);
    draftFields.setAdvancedOpen(false);
    draft.reset();
    author.reset();
    approve.reset();
  }

  return (
    <section className="rounded-card border border-hairline bg-surface-1 p-body">
      <div className="flex flex-col gap-section">
        <DescribeStep
          description={draftFields.description}
          onDescriptionChange={draftFields.setDescription}
          onSubmit={handleDescribeSubmit}
          canDraft={canDraft}
          isDrafting={draft.isPending}
          draftIsError={draft.isError}
          draftError={draft.error}
          advancedOpen={draftFields.advancedOpen}
          hasResult={Boolean(draftFields.result)}
          onOpenAdvanced={() => draftFields.setAdvancedOpen(true)}
        />

        {draftFields.result && (
          <DraftReviewPanel
            result={draftFields.result}
            approveIsPending={approve.isPending}
            approveIsError={approve.isError}
            approveError={approve.error}
            onApprove={handleApprove}
            onEditDetails={handleEditDetails}
            onStartOver={handleStartOver}
          />
        )}

        {draftFields.advancedOpen && (
          <AdvancedAuthorForm
            hasResult={Boolean(draftFields.result)}
            onClose={() => draftFields.setAdvancedOpen(false)}
            contextIsLoading={context.isLoading}
            contextIsError={context.isError}
            authoring={authoring}
            draftFields={draftFields}
            paramsField={paramsField}
            mcpField={mcpField}
            verification={verification}
            writableMcpNeedsPolicy={writableMcpNeedsPolicy}
            authorIsError={author.isError}
            authorError={author.error}
            authorIsPending={author.isPending}
            canSubmit={canSubmit}
            onSubmit={handleSubmit}
          />
        )}
      </div>
    </section>
  );
}
