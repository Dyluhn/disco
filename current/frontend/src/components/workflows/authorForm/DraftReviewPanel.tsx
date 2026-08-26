import { AlertTriangle, Check, CheckCircle2, Loader2, Pencil, RotateCcw } from "lucide-react";
import type { WorkflowDraftFromDescriptionResult } from "@/types/workflow";
import { authorInputFromReview, errorMessage, outputLabel, workflowUses } from "./draftMapping";
import { FindingRows } from "./FindingRows";

interface DraftReviewPanelProps {
  result: WorkflowDraftFromDescriptionResult;
  approveIsPending: boolean;
  approveIsError: boolean;
  approveError: unknown;
  onApprove: () => void;
  onEditDetails: () => void;
  onStartOver: () => void;
}

export function DraftReviewPanel({
  result,
  approveIsPending,
  approveIsError,
  approveError,
  onApprove,
  onEditDetails,
  onStartOver,
}: DraftReviewPanelProps) {
  const uses = workflowUses(result.workflow);
  const resultInput = authorInputFromReview(result.workflow);
  const errorFindings = result.workflow.validation_findings.some(
    (finding) => finding.severity === "error",
  );

  return (
    <section
      aria-label={`${result.workflow.name} draft review`}
      className="flex flex-col gap-inline rounded-control border border-hairline bg-surface-2 p-inline"
    >
      <div className="flex flex-col gap-hair">
        <div className="font-ui text-[0.82rem] text-text-muted">{result.summary}</div>
        <h3 className="font-ui text-[0.96rem] font-semibold text-text">{result.workflow.name}</h3>
        <dl className="grid gap-hair font-ui text-[0.8rem] text-text-muted md:grid-cols-[7rem_1fr]">
          <dt className="text-text-faint">Uses</dt>
          <dd className="text-text">{uses.length > 0 ? uses.join(", ") : "No tools"}</dd>
          <dt className="text-text-faint">Produces</dt>
          <dd className="text-text">
            {outputLabel(result)} ({result.simulation.output_format ?? "unknown"})
          </dd>
          <dt className="text-text-faint">Asks for</dt>
          <dd className="text-text">
            {resultInput.params.length > 0
              ? resultInput.params
                  .map((param) => `${param.name}${param.required ? "" : " (optional)"}`)
                  .join(", ")
              : "No parameters"}
          </dd>
        </dl>
      </div>

      <section className="flex flex-col gap-hair">
        <h4 className="font-ui text-[0.86rem] font-semibold text-text">Configuration checks</h4>
        <FindingRows findings={result.workflow.validation_findings} />
        {errorFindings && (
          <div className="font-ui text-[0.78rem] text-unsupported">
            These checks need attention before this capability can be enabled.
          </div>
        )}
      </section>

      <div className="flex items-center gap-hair font-ui text-[0.8rem] text-text-muted">
        {result.simulation.ok ? (
          <CheckCircle2 className="size-4 text-supported" aria-hidden />
        ) : (
          <AlertTriangle className="size-4 text-warn" aria-hidden />
        )}
        Configuration checks {result.simulation.ok ? "pass" : "need attention"}
      </div>

      {approveIsError && (
        <div
          role="alert"
          className="flex items-start gap-hair rounded-control border border-unsupported/50 bg-surface-1 p-inline font-ui text-[0.82rem] text-unsupported"
        >
          <AlertTriangle className="size-4 shrink-0" aria-hidden />
          {errorMessage(approveError, "Workflow approval failed.")}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-hair">
        <button
          type="button"
          onClick={onApprove}
          disabled={errorFindings || approveIsPending || result.workflow.approved}
          className="inline-flex min-h-11 w-fit items-center justify-center gap-hair rounded-control border border-hairline px-body py-hair font-ui text-[0.84rem] text-text-muted transition-colors hover:border-accent/60 hover:text-text disabled:cursor-not-allowed disabled:opacity-50 lg:min-h-0"
        >
          {approveIsPending ? (
            <Loader2 className="size-3.5 animate-spin" aria-hidden />
          ) : (
            <Check className="size-3.5" aria-hidden />
          )}
          {result.workflow.approved ? "Enabled" : "Create & enable"}
        </button>
        <button
          type="button"
          onClick={onEditDetails}
          className="inline-flex min-h-11 w-fit items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted hover:text-text lg:min-h-0"
        >
          <Pencil className="size-3.5" aria-hidden />
          Edit details
        </button>
        <button
          type="button"
          onClick={onStartOver}
          className="inline-flex min-h-11 w-fit items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted hover:text-text lg:min-h-0"
        >
          <RotateCcw className="size-3.5" aria-hidden />
          Start over
        </button>
      </div>
    </section>
  );
}
