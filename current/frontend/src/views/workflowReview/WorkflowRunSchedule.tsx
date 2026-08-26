import { Loader2, Play } from "lucide-react";
import { Link } from "react-router-dom";
import { useId, useState } from "react";
import { useToast } from "@/components/toastApi";
import { useConversations } from "@/hooks/useConversations";
import { useRunWorkflow } from "@/hooks/useWorkflows";
import { initialWorkflowParams, parseWorkflowParams, type WorkflowParamSchema } from "@/lib/workflowParams";
import type { WorkflowReview } from "@/types/workflow";

/**
 * The simple, truthful run surface for an approved workflow.
 *
 * Running a workflow is the primary action. Scheduling is still supported, but
 * it is an explicit secondary disclosure so a raw scheduler control can never
 * compete with the first-run path. The API payloads are unchanged.
 */
export function WorkflowRunSchedule({ workflow }: { workflow: WorkflowReview }) {
  const run = useRunWorkflow();
  const conversations = useConversations();
  const toast = useToast();
  const targetId = useId();
  const [conversationId, setConversationId] = useState("");
  const [params, setParams] = useState<Record<string, string>>(() =>
    initialWorkflowParams(
      workflow.compiled_surface.params_model_schema as WorkflowParamSchema,
      workflow.params,
    ),
  );
  const [paramErrors, setParamErrors] = useState<Record<string, string>>({});
  const [lastConversationId, setLastConversationId] = useState<string | null>(null);

  const schema = workflow.compiled_surface.params_model_schema as WorkflowParamSchema;
  const properties = Object.entries(schema.properties ?? {});
  const runPending = run.isPending;

  function handleRun() {
    const parsed = parseWorkflowParams(schema, params);
    const errors = parsed.errors;
    setParamErrors(errors);
    if (Object.keys(errors).length > 0) return;
    run.mutate(
      {
        instanceId: workflow.instance_id,
        params: parsed.params,
        conversationId: conversationId || null,
      },
      {
        onSuccess: (result) => {
          setLastConversationId(result.conversation_id);
          toast.show({
            title: "Workflow run started",
            body: result.conversation_id,
          });
        },
      },
    );
  }

  return (
    <section className="flex flex-col gap-inline rounded-control border border-hairline bg-surface-2 p-inline">
      <div className="flex flex-col gap-hair">
        <h3 className="font-ui text-[0.86rem] font-semibold text-text">Run workflow</h3>
        <p className="font-ui text-[0.76rem] text-text-faint">
          Start this capability in an Agent conversation.
        </p>
      </div>

      <div className="flex flex-wrap items-end gap-inline">
        <div className="flex min-w-[15rem] flex-col gap-px">
          <label
            htmlFor={targetId}
            className="font-ui text-[0.72rem] uppercase tracking-wide text-text-faint"
          >
            Agent conversation
          </label>
          <select
            id={targetId}
            aria-label={`Agent conversation for ${workflow.name}`}
            value={conversationId}
            onChange={(event) => setConversationId(event.target.value)}
            className="min-h-11 rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.78rem] text-text outline-none focus:border-accent lg:min-h-0"
          >
            <option value="">New Agent conversation</option>
            {(conversations.data ?? [])
              .filter((item) => item.surface === "agent" && item.origin !== "imported")
              .map((item) => (
                <option key={item.id} value={item.id}>
                  {item.title || item.id}
                </option>
              ))}
          </select>
        </div>

        <div className="flex min-w-0 flex-1 flex-col gap-hair">
          <span className="font-ui text-[0.72rem] uppercase tracking-wide text-text-faint">
            Required inputs
          </span>
          <div className="flex flex-wrap items-end gap-hair">
            {properties.length === 0 && (
              <span className="font-ui text-[0.78rem] text-text-muted">None</span>
            )}
            {properties.map(([name, definition]) => (
              <label
                key={name}
                className="flex min-w-[10rem] flex-1 flex-col gap-px font-ui text-[0.72rem] text-text-faint"
              >
                <span>
                  {name}
                  {(schema.required ?? []).includes(name) ? " *" : ""}
                </span>
                <input
                  aria-label={`Workflow parameter ${name}`}
                  value={params[name] ?? ""}
                  onChange={(event) => {
                    setParams((current) => ({ ...current, [name]: event.target.value }));
                    setParamErrors((current) => ({ ...current, [name]: "" }));
                  }}
                  placeholder={definition.description ?? definition.type ?? "value"}
                  className="min-h-11 rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.78rem] text-text outline-none focus:border-accent lg:min-h-0"
                />
                {paramErrors[name] && <span className="text-unsupported">{paramErrors[name]}</span>}
              </label>
            ))}
          </div>
        </div>

        <button
          type="button"
          onClick={handleRun}
          disabled={runPending}
          className="inline-flex min-h-11 items-center justify-center gap-hair rounded-control border border-hairline px-body py-hair font-ui text-[0.82rem] text-text-muted transition-colors hover:border-accent/60 hover:text-text disabled:cursor-not-allowed disabled:opacity-50 lg:min-h-0"
        >
          {runPending ? (
            <Loader2 className="size-3.5 animate-spin" aria-hidden />
          ) : (
            <Play className="size-3.5" aria-hidden />
          )}
          Run
        </button>
        {lastConversationId && (
          <Link
            to={`/agent/${encodeURIComponent(lastConversationId)}`}
            className="font-ui text-[0.78rem] text-accent hover:underline"
          >
            Open Agent conversation
          </Link>
        )}
      </div>

      {run.isError && (
        <div role="alert" className="font-ui text-[0.78rem] text-unsupported">
          Workflow run could not be started.
        </div>
      )}
    </section>
  );
}
