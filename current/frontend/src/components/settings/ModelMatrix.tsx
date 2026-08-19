import { CapabilityBadges } from "@/components/CapabilityBadges";
import { cn } from "@/lib/cn";
import { costLabel } from "@/lib/cost";
import {
  findModel,
  useAssignments,
  useModels,
  useUpdateAssignments,
} from "@/hooks/useModels";
import { isFree, type ModelInfo, ROLES } from "@/types/models";
import { RoleModelPicker } from "./RoleModelPicker";

/**
 * The model-assignment matrix (Prompt 4, the core). Model selection here is
 * ABSOLUTE and MANUAL — there is no automatic routing. A default primary (the
 * conversation lead, overridable per conversation by the main-screen pill) plus an
 * explicit selector for every other role. Each row surfaces the assigned model's
 * capabilities (advisory metadata, fail-loud at runtime — the UI predicts/blocks
 * nothing) and cost, so the user assigns with capability and spend in view.
 */
interface RowSpec {
  key: string;
  label: string;
  desc: string;
  modelId: string | undefined;
  onSelect: (id: string) => void;
}

function MatrixRow({
  row,
  models,
  busy,
}: {
  row: RowSpec;
  models: ModelInfo[] | undefined;
  busy: boolean;
}) {
  const model = findModel(models, row.modelId ?? null);
  return (
    <div className="flex flex-col gap-inline border-b border-hairline py-body last:border-b-0 sm:flex-row sm:items-start sm:justify-between sm:gap-section">
      <div className="sm:max-w-xs">
        <div className="font-ui text-[0.9rem] font-medium text-text">
          {row.label}
        </div>
        <p className="font-ui text-[0.8rem] leading-snug text-text-muted">
          {row.desc}
        </p>
      </div>
      <div className="flex flex-col gap-hair sm:items-end">
        <RoleModelPicker
          value={row.modelId ?? ""}
          onSelect={(id) => {
            if (id !== null) row.onSelect(id);
          }}
          ariaLabel={`Choose model for ${row.label}`}
          busy={busy}
        />
        <div className="flex flex-wrap items-center gap-inline sm:justify-end">
          {model && (
            <span
              className={cn(
                "font-ui text-[0.74rem]",
                isFree(model) ? "text-text-muted" : "text-accent",
              )}
            >
              {costLabel(model)}
            </span>
          )}
          <CapabilityBadges capabilities={model?.capabilities ?? []} />
        </div>
      </div>
    </div>
  );
}

function VisualModelRow({
  modelId,
  model,
  busy,
  onSelect,
}: {
  modelId: string | null;
  model: ModelInfo | null;
  busy: boolean;
  onSelect: (id: string | null) => void;
}) {
  return (
    <div className="flex flex-col gap-inline border-b border-hairline py-body sm:flex-row sm:items-start sm:justify-between sm:gap-section">
      <div className="sm:max-w-xs">
        <div className="font-ui text-[0.9rem] font-medium text-text">
          Visual inspection (optional)
        </div>
        <p className="font-ui text-[0.8rem] leading-snug text-text-muted">
          Receives one screenshot and one focused question, with no tools or
          conversation history, then returns advisory text to the main model. It
          never takes over the agent.
        </p>
      </div>
      <div className="flex flex-col gap-hair sm:items-end">
        <RoleModelPicker
          value={modelId}
          onSelect={onSelect}
          ariaLabel="Choose optional visual inspection model"
          busy={busy}
          noneLabel="Use main model when capable"
        />
        {model && (
          <div className="flex flex-wrap items-center gap-inline sm:justify-end">
            <span
              className={cn(
                "font-ui text-[0.74rem]",
                isFree(model) ? "text-text-muted" : "text-accent",
              )}
            >
              {costLabel(model)}
            </span>
            <CapabilityBadges capabilities={model.capabilities} />
          </div>
        )}
      </div>
    </div>
  );
}

function VisualModelGuidance({
  primary,
  visualId,
  visual,
}: {
  primary: ModelInfo | null;
  visualId: string | null;
  visual: ModelInfo | null;
}) {
  const stateOf = (model: ModelInfo | null) => {
    // A placeholder/non-runnable entry is already covered by the driver setup
    // warning. It has no capability verdict, so never call it text-only here.
    if (!model?.base_url) {
      return null;
    }
    if (model.vision === true) return "vision";
    if (model.vision === false) return "text-only";
    if (model.vision_status) return model.vision_status;
    return model.capabilities.includes("vision") ? "vision" : "unknown";
  };
  const state = stateOf(visualId ? visual : primary);
  if (state === null || state === "vision") return null;
  const selected = visualId ? visual : primary;
  const target = visualId ? "selected visual model" : "primary model";
  const textOnly = state === "text-only";

  return (
    <div
      role="status"
      data-disco-flag={
        textOnly ? "vision-model-text-only" : "vision-model-unverified"
      }
      className="grid gap-hair rounded-control border border-warn/40 bg-warn/5 px-body py-inline font-ui text-[0.8rem] leading-snug text-text-muted sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center sm:gap-body"
    >
      <span>
        <span className="font-medium text-text">
          {textOnly
            ? `${selected?.label ?? target} is marked text-only.`
            : `Vision hasn't been verified for ${selected?.label ?? target}.`}
        </span>{" "}
        {textOnly
          ? "Choose an image-capable model or correct this model's capability."
          : "If it accepts images, confirm that capability; otherwise choose a dedicated visual model."}
      </span>
      <a
        href="#model-library"
        onClick={() => {
          const disclosure = document.querySelector<HTMLDetailsElement>(
            "#model-library details",
          );
          if (disclosure) disclosure.open = true;
        }}
        className="font-medium text-accent hover:underline"
      >
        Model library → Edit → Image understanding
      </a>
    </div>
  );
}

export function ModelMatrix() {
  const { data: assignments, isLoading, isError } = useAssignments();
  const { data: models } = useModels();
  const update = useUpdateAssignments();
  const primary = findModel(models, assignments?.default_model ?? null);
  const visual = findModel(models, assignments?.vision_model ?? null);

  return (
    <section
      aria-labelledby="models-heading"
      className="flex flex-col gap-inline"
    >
      <div>
        <h3
          id="models-heading"
          className="font-ui text-[0.95rem] font-semibold text-text"
        >
          Role assignments
        </h3>
        <p className="font-ui text-[0.84rem] text-text-muted">
          Choose which existing model handles each role. Add or edit the available
          models in Model library below. Assignments are manual and apply on the
          next request.
        </p>
      </div>

      <div className="rounded-card border border-hairline bg-surface-1 px-body">
        {isLoading && (
          <div className="py-body font-ui text-[0.84rem] text-text-muted">
            Loading models…
          </div>
        )}
        {isError && (
          <div className="py-body font-ui text-[0.84rem] text-warn">
            Couldn't load assignments.
          </div>
        )}
        {assignments && (
          <>
            <MatrixRow
              row={{
                key: "default",
                label: "Default primary (lead)",
                desc: "Leads conversations unless the main-screen model pill overrides it.",
                modelId: assignments.default_model,
                onSelect: (id) => update.mutate({ default_model: id }),
              }}
              models={models}
              busy={update.isPending}
            />
            <VisualModelRow
              modelId={assignments.vision_model}
              model={visual}
              busy={update.isPending}
              onSelect={(id) => update.mutate({ vision_model: id })}
            />
            <details className="py-inline">
              <summary className="cursor-pointer py-3 font-ui text-[0.84rem] font-medium text-text lg:py-0">
                Specialist role overrides
              </summary>
              <p className="mt-hair font-ui text-[0.76rem] text-text-faint">
                Optional fixed models for retrieval and synthesis tasks.
              </p>
              <div className="mt-inline border-t border-hairline">
                {ROLES.map((role) => (
                  <MatrixRow
                    key={role.id}
                    row={{
                      key: role.id,
                      label: role.label,
                      desc: role.description,
                      modelId: assignments.roles[role.id],
                      onSelect: (id) =>
                        update.mutate({ roles: { [role.id]: id } }),
                    }}
                    models={models}
                    busy={update.isPending}
                  />
                ))}
              </div>
            </details>
          </>
        )}
      </div>

      {assignments && (
        <VisualModelGuidance
          primary={primary}
          visualId={assignments.vision_model}
          visual={visual}
        />
      )}

      {update.error && (
        <p role="alert" className="font-ui text-[0.8rem] text-unsupported">
          Couldn't save the assignment —{" "}
          {update.error instanceof Error
            ? update.error.message
            : "the server didn't respond"}
          . Your change wasn't applied.
        </p>
      )}
    </section>
  );
}
