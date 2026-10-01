import { Pencil, Plus, Trash2 } from "lucide-react";
import { useRef, useState } from "react";
import { isApiFailure } from "@/api/errors";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { cn } from "@/lib/cn";
import { costLabel } from "@/lib/cost";
import { TAP_TARGET_ICON } from "@/lib/tapTarget";
import { useDeleteModel, useModels } from "@/hooks/useModels";
import { isMetered, type ModelUpsert } from "@/types/models";
import { BLANK, FormDialog, toUpsert } from "./ModelCatalogueForm";

/**
 * The model CATALOGUE — add, edit, and remove the assignable models. Real CRUD:
 * each change persists to the shared config the agent-server routes through, so a
 * model added here is immediately assignable in the matrix above and callable at
 * runtime. The form edits the raw config (endpoint, model id, context, key env).
 */
export function ModelCatalogue() {
  const { data: models } = useModels();
  const del = useDeleteModel();
  const addButtonRef = useRef<HTMLButtonElement | null>(null);
  const [dialog, setDialog] = useState<{
    mode: "add" | "edit";
    initial: ModelUpsert;
    trigger: HTMLButtonElement | null;
  } | null>(null);

  return (
    <details className="rounded-card border border-hairline bg-surface-1/30 px-body py-inline">
      <summary className="cursor-pointer list-none">
        <span className="flex items-center justify-between gap-inline">
          <span className="flex min-w-0 flex-col gap-hair">
            <span className="font-ui text-[0.9rem] font-semibold text-text">
              Model library
            </span>
            <span className="font-ui text-[0.76rem] font-normal leading-snug text-text-faint">
              Add or edit the models that can be selected in Role assignments.
            </span>
          </span>
          <span className="shrink-0 font-ui text-[0.76rem] text-text-muted">
            {(models ?? []).length} models
          </span>
        </span>
      </summary>

      <div className="mt-inline flex flex-col gap-inline border-t border-hairline pt-inline">
        <div className="flex justify-end">
          <button
            type="button"
            data-disco-control="settings.model-add"
            ref={addButtonRef}
            onClick={(event) =>
              setDialog({ mode: "add", initial: BLANK, trigger: event.currentTarget })
            }
            className="flex min-h-11 items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.8rem] text-text-muted transition-colors hover:border-hairline-strong hover:text-text lg:min-h-0"
          >
            <Plus className="size-3.5" aria-hidden /> Add model
          </button>
        </div>

        <ul className="overflow-hidden rounded-card border border-hairline bg-surface-1">
          {(models ?? []).map((m) => (
            <li
              key={m.id}
              className="flex items-center justify-between gap-section border-b border-hairline px-body py-inline last:border-b-0"
            >
              <div className="min-w-0">
                <div className="font-ui text-[0.86rem] font-medium text-text">
                  {m.label}
                </div>
                {/* Wraps on mobile so the id is fully readable (no room for a
                    hover tooltip on touch); lg:truncate restores the original
                    single-line ellipsis once the desktop sidebar gives it a
                    fixed, comfortably wide column. */}
                <p className="break-words font-mono text-[0.72rem] text-text-faint lg:truncate">
                  {m.id} · {m.note}
                </p>
              </div>
              <div className="flex shrink-0 items-center gap-inline">
                {/* W-05: the row shows its pricing via the SAME formatter every other cost
                    surface uses — a subscription model reads "Subscription" here too (never
                    "Free", never a $/Mtok rate); free/metered show "Free"/the price. */}
                <span
                  data-disco-flag="catalogue-cost"
                  className={cn(
                    "shrink-0 font-ui text-[0.72rem]",
                    isMetered(m) ? "text-accent" : "text-text-muted",
                  )}
                >
                  {costLabel(m)}
                </span>
                <button
                  type="button"
                  data-disco-control="settings.model-edit"
                  data-model-id={m.id}
                  aria-label={`Edit ${m.id}`}
                  onClick={(event) =>
                    setDialog({
                      mode: "edit",
                      initial: toUpsert(m),
                      trigger: event.currentTarget,
                    })
                  }
                  className={cn(
                    "rounded-control p-hair text-text-faint transition-colors hover:text-text",
                    TAP_TARGET_ICON,
                  )}
                >
                  <Pencil className="size-3.5" aria-hidden />
                </button>
                {/* Driveable confirm (replaces native confirm(), which Playwright/the
                    harness cannot address) — the in-app ConfirmDialog gate. */}
                <ConfirmDialog
                  title={`Remove ${m.id}?`}
                  description={`This removes ${m.id} from the catalogue. It will no longer be assignable or callable at runtime.`}
                  confirmLabel="Remove model"
                  onConfirm={() => del.mutate(m.id)}
                  trigger={
                    <button
                      type="button"
                      data-disco-control="settings.model-delete"
                      data-model-id={m.id}
                      aria-label={`Remove ${m.id}`}
                      className={cn(
                        "rounded-control p-hair text-text-faint transition-colors hover:text-unsupported",
                        TAP_TARGET_ICON,
                      )}
                    >
                      <Trash2 className="size-3.5" aria-hidden />
                    </button>
                  }
                />
              </div>
            </li>
          ))}
        </ul>
        {del.error && (
          <p role="alert" className="font-ui text-[0.8rem] text-unsupported">
            {isApiFailure(del.error)
              ? del.error.message
              : "Couldn't remove the model."}
          </p>
        )}

        {dialog && (
          <FormDialog
            open
            onOpenChange={(o) => !o && setDialog(null)}
            mode={dialog.mode}
            initial={dialog.initial}
            trigger={dialog.trigger}
            addButtonRef={addButtonRef}
          />
        )}
      </div>
    </details>
  );
}
