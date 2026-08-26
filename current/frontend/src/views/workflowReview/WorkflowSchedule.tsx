import { CalendarClock, Check, ChevronDown, Loader2 } from "lucide-react";
import { useId, useState } from "react";
import { useToast } from "@/components/toastApi";
import { useScheduleWorkflow } from "@/hooks/useWorkflows";
import { parseScheduleNL } from "@/lib/scheduleNL";
import { browserScheduleTimezone } from "@/lib/scheduleLocal";
import {
  initialWorkflowParams,
  parseWorkflowParams,
  type WorkflowParamSchema,
} from "@/lib/workflowParams";
import type { WorkflowReview } from "@/types/workflow";

/** Scheduling is an expert capability and is intentionally not mounted in the default Run card. */
export function WorkflowSchedule({ workflow }: { workflow: WorkflowReview }) {
  const schedule = useScheduleWorkflow();
  const toast = useToast();
  const schedulingId = useId();
  const [scheduleOpen, setScheduleOpen] = useState(false);
  const [scheduleText, setScheduleText] = useState("");
  const [scheduleDescription, setScheduleDescription] = useState<string | null>(null);
  const [scheduleError, setScheduleError] = useState<string | null>(null);
  const schema = workflow.compiled_surface.params_model_schema as WorkflowParamSchema;
  const properties = Object.entries(schema.properties ?? {});
  const [params, setParams] = useState<Record<string, string>>(() =>
    initialWorkflowParams(schema, workflow.params),
  );
  const [paramErrors, setParamErrors] = useState<Record<string, string>>({});
  const schedulePending =
    schedule.isPending && schedule.variables?.instanceId === workflow.instance_id;

  function handleSchedule() {
    const parsed = parseScheduleNL(scheduleText);
    if (!parsed.success || !parsed.rrule) {
      setScheduleError(parsed.error || "Enter a schedule.");
      return;
    }
    const workflowParams = parseWorkflowParams(schema, params);
    setParamErrors(workflowParams.errors);
    if (Object.keys(workflowParams.errors).length > 0) return;
    setScheduleError(null);
    schedule.mutate(
      {
        instanceId: workflow.instance_id,
        instanceDigest: workflow.definition_digest,
        cron: parsed.rrule,
        timezone: browserScheduleTimezone(),
        params: workflowParams.params,
      },
      {
        onSuccess: () => {
          setScheduleDescription(parsed.description);
          toast.show({ title: "Workflow schedule saved", body: parsed.description });
        },
      },
    );
  }

  return (
    <div className="rounded-control border border-hairline bg-surface-1">
      <button
        type="button"
        aria-expanded={scheduleOpen}
        aria-controls={schedulingId}
        onClick={() => setScheduleOpen((open) => !open)}
        className="group flex min-h-11 w-full items-center gap-hair px-inline py-hair text-left font-ui text-[0.78rem] text-text-muted hover:text-text lg:min-h-0"
      >
        <ChevronDown
          className="size-3.5 shrink-0 text-text-faint transition-transform group-aria-expanded:rotate-180"
          aria-hidden
        />
        <CalendarClock className="size-3.5 shrink-0 text-text-faint" aria-hidden />
        <span>Scheduling</span>
        {scheduleDescription && <span className="text-text-faint">· {scheduleDescription}</span>}
      </button>
      {scheduleOpen && (
        <div
          id={schedulingId}
          className="flex flex-col gap-inline border-t border-hairline p-inline"
        >
          <p className="font-ui text-[0.76rem] text-text-faint">
            Choose when Agent should run this workflow. Try “every weekday”, “daily at 9am”, or “every 2 hours”.
            Saving again replaces this workflow&apos;s existing schedule.
          </p>
          <label className="flex flex-col gap-hair font-ui text-[0.78rem] text-text-muted">
            Schedule
            <input
              aria-label={`Schedule for ${workflow.name}`}
              value={scheduleText}
              onChange={(event) => {
                setScheduleText(event.target.value);
                setScheduleError(null);
              }}
              placeholder="e.g. every weekday or daily at 9am"
              className="min-h-11 rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.82rem] text-text outline-none focus:border-accent lg:min-h-0"
            />
          </label>
          {properties.length > 0 && (
            <div className="flex flex-wrap items-end gap-hair">
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
                    aria-label={`Schedule workflow parameter ${name}`}
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
          )}
          {scheduleError && (
            <p role="alert" className="font-ui text-[0.78rem] text-unsupported">
              {scheduleError}
            </p>
          )}
          {schedule.isError && (
            <p role="alert" className="font-ui text-[0.78rem] text-unsupported">
              Workflow schedule could not be saved.
            </p>
          )}
          <button
            type="button"
            onClick={handleSchedule}
            disabled={schedulePending || !scheduleText.trim()}
            className="inline-flex min-h-11 w-fit items-center justify-center gap-hair rounded-control border border-hairline px-body py-hair font-ui text-[0.82rem] text-text-muted transition-colors hover:border-accent/60 hover:text-text disabled:cursor-not-allowed disabled:opacity-50 lg:min-h-0"
          >
            {schedulePending ? (
              <Loader2 className="size-3.5 animate-spin" aria-hidden />
            ) : (
              <Check className="size-3.5" aria-hidden />
            )}
            Save schedule
          </button>
        </div>
      )}
    </div>
  );
}
