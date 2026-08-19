import { Plus, Trash2 } from "lucide-react";
import type { WorkflowAuthoringContext, WorkflowDraftParam } from "@/types/workflow";
import type { ParamType } from "./constants";

interface ParamsFieldProps {
  params: WorkflowDraftParam[];
  paramTypes: WorkflowAuthoringContext["param_types"];
  duplicateParams: boolean;
  invalidParam: boolean;
  onAdd: () => void;
  onUpdate: (index: number, patch: Partial<WorkflowDraftParam>) => void;
  onRemove: (index: number) => void;
}

export function ParamsField({
  params,
  paramTypes,
  duplicateParams,
  invalidParam,
  onAdd,
  onUpdate,
  onRemove,
}: ParamsFieldProps) {
  return (
    <section className="flex flex-col gap-inline">
      <div className="flex items-center justify-between gap-inline">
        <h4 className="font-ui text-[0.86rem] font-semibold text-text">Parameters</h4>
        <button
          type="button"
          onClick={onAdd}
          className="inline-flex min-h-11 items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted hover:text-text lg:min-h-0"
        >
          <Plus className="size-3.5" aria-hidden />
          Add
        </button>
      </div>
      <div className="flex flex-col gap-hair">
        {params.map((param, index) => (
          <div key={index} className="grid gap-hair md:grid-cols-[1fr_10rem_6rem_1fr_auto]">
            <input
              aria-label={`Parameter ${index + 1} name`}
              value={param.name}
              onChange={(event) => onUpdate(index, { name: event.target.value })}
              placeholder="name"
              className="min-h-11 rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.82rem] text-text outline-none focus:border-accent lg:min-h-0"
            />
            <select
              aria-label={`Parameter ${index + 1} type`}
              value={param.type}
              onChange={(event) => onUpdate(index, { type: event.target.value as ParamType })}
              className="min-h-11 rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.82rem] text-text outline-none focus:border-accent lg:min-h-0"
            >
              {paramTypes.map((type) => (
                <option key={type} value={type}>
                  {type}
                </option>
              ))}
            </select>
            <label className="flex min-h-11 items-center gap-hair rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.78rem] text-text-muted lg:min-h-0">
              <input
                type="checkbox"
                checked={param.required}
                onChange={(event) => onUpdate(index, { required: event.target.checked })}
              />
              Required
            </label>
            <input
              aria-label={`Parameter ${index + 1} description`}
              value={param.description ?? ""}
              onChange={(event) => onUpdate(index, { description: event.target.value })}
              placeholder="description"
              className="min-h-11 rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.82rem] text-text outline-none focus:border-accent lg:min-h-0"
            />
            <button
              type="button"
              onClick={() => onRemove(index)}
              aria-label={`Remove parameter ${index + 1}`}
              className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-control border border-hairline px-inline py-hair text-text-muted hover:text-unsupported lg:min-h-0 lg:min-w-0"
            >
              <Trash2 className="size-4" aria-hidden />
            </button>
          </div>
        ))}
      </div>
      {(duplicateParams || invalidParam) && (
        <div className="font-ui text-[0.78rem] text-unsupported">
          Parameter names must be present and unique.
        </div>
      )}
    </section>
  );
}
