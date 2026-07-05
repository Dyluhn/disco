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
          onSelect={row.onSelect}
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

export function ModelMatrix() {
  const { data: assignments, isLoading, isError } = useAssignments();
  const { data: models } = useModels();
  const update = useUpdateAssignments();

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
          Read live from the configured deployment. Assignments are absolute and
          manual — the system uses exactly what you set, applied on the next
          request (no automatic routing). Capabilities are advisory; a
          mis-assignment fails loudly at runtime.
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
            {ROLES.map((role) => (
              <MatrixRow
                key={role.id}
                row={{
                  key: role.id,
                  label: role.label,
                  desc: role.description,
                  modelId: assignments.roles[role.id],
                  onSelect: (id) => update.mutate({ roles: { [role.id]: id } }),
                }}
                models={models}
                busy={update.isPending}
              />
            ))}
          </>
        )}
      </div>

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
