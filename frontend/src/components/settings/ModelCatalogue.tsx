import * as Dialog from "@radix-ui/react-dialog";
import { Pencil, Plus, Trash2 } from "lucide-react";
import { type FormEvent, useState } from "react";
import { ApiError } from "@/api/client";
import { cn } from "@/lib/cn";
import { useCreateModel, useDeleteModel, useModels, useUpdateModel } from "@/hooks/useModels";
import { type Capability, CAPABILITY_LABEL, type ModelInfo, type ModelUpsert } from "@/types/models";

/**
 * The model CATALOGUE — add, edit, and remove the assignable models. Real CRUD:
 * each change persists to the shared config the agent-server routes through, so a
 * model added here is immediately assignable in the matrix above and callable at
 * runtime. The form edits the raw config (endpoint, model id, context, key env).
 */
const CAPS: Capability[] = ["tool_calling", "json_mode", "long_context", "vision"];
const BLANK: ModelUpsert = {
  id: "",
  model_id: "",
  base_url: "",
  api_key_env: "",
  context_window: 8192,
  quantization: "",
  capabilities: [],
  price_in_per_m: 0,
  price_out_per_m: 0,
};

function toUpsert(m: ModelInfo): ModelUpsert {
  return {
    id: m.id,
    model_id: m.model_id,
    base_url: m.base_url ?? "",
    api_key_env: m.api_key_env ?? "",
    context_window: m.context_window,
    quantization: m.quantization ?? "",
    capabilities: [...m.capabilities],
    price_in_per_m: m.price_in_per_m,
    price_out_per_m: m.price_out_per_m,
  };
}

const field =
  "w-full rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.84rem] text-text outline-none focus:border-hairline-strong";
const labelCls = "font-ui text-[0.74rem] font-medium uppercase tracking-wide text-text-faint";

function ModelForm({
  mode,
  initial,
  onDone,
}: {
  mode: "add" | "edit";
  initial: ModelUpsert;
  onDone: () => void;
}) {
  const [form, setForm] = useState<ModelUpsert>(initial);
  const create = useCreateModel();
  const update = useUpdateModel();
  const busy = create.isPending || update.isPending;
  const err = (create.error ?? update.error) as ApiError | Error | null;
  const set = <K extends keyof ModelUpsert>(k: K, v: ModelUpsert[K]) =>
    setForm((f) => ({ ...f, [k]: v }));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const payload: ModelUpsert = {
      ...form,
      base_url: form.base_url?.trim() || null,
      api_key_env: form.api_key_env?.trim() || null,
      quantization: form.quantization?.trim() || null,
    };
    try {
      if (mode === "add") await create.mutateAsync(payload);
      else await update.mutateAsync({ id: form.id, upsert: payload });
      onDone();
    } catch {
      /* error shown inline below */
    }
  };

  return (
    <form onSubmit={submit} className="flex flex-col gap-body">
      <div className="grid grid-cols-2 gap-body">
        <label className="flex flex-col gap-hair">
          <span className={labelCls}>Catalogue id</span>
          <input
            className={cn(field, mode === "edit" && "opacity-60")}
            value={form.id}
            disabled={mode === "edit"}
            placeholder="my-llama"
            onChange={(e) => set("id", e.target.value.trim())}
            required
          />
        </label>
        <label className="flex flex-col gap-hair">
          <span className={labelCls}>Model id (sent to the API)</span>
          <input
            className={field}
            value={form.model_id}
            placeholder="llama-3.3-70b.gguf"
            onChange={(e) => set("model_id", e.target.value)}
            required
          />
        </label>
        <label className="col-span-2 flex flex-col gap-hair">
          <span className={labelCls}>Endpoint base URL</span>
          <input
            className={field}
            value={form.base_url ?? ""}
            placeholder="http://localhost:8080/v1"
            onChange={(e) => set("base_url", e.target.value)}
          />
        </label>
        <label className="flex flex-col gap-hair">
          <span className={labelCls}>API key env var (optional)</span>
          <input
            className={field}
            value={form.api_key_env ?? ""}
            placeholder="PMX_OPENROUTER_API_KEY"
            onChange={(e) => set("api_key_env", e.target.value)}
          />
        </label>
        <label className="flex flex-col gap-hair">
          <span className={labelCls}>Context window</span>
          <input
            type="number"
            className={field}
            value={form.context_window}
            min={1}
            onChange={(e) => set("context_window", Number(e.target.value))}
            required
          />
        </label>
        <label className="flex flex-col gap-hair">
          <span className={labelCls}>Quantization (optional)</span>
          <input
            className={field}
            value={form.quantization ?? ""}
            placeholder="Q5_K_XL"
            onChange={(e) => set("quantization", e.target.value)}
          />
        </label>
      </div>

      <fieldset className="flex flex-col gap-hair">
        <span className={labelCls}>Capabilities (advisory)</span>
        <div className="flex flex-wrap gap-inline">
          {CAPS.map((c) => (
            <label key={c} className="flex items-center gap-hair font-ui text-[0.8rem] text-text-muted">
              <input
                type="checkbox"
                checked={form.capabilities.includes(c)}
                onChange={(e) =>
                  set(
                    "capabilities",
                    e.target.checked
                      ? [...form.capabilities, c]
                      : form.capabilities.filter((x) => x !== c),
                  )
                }
              />
              {CAPABILITY_LABEL[c]}
            </label>
          ))}
        </div>
      </fieldset>

      <div className="grid grid-cols-2 gap-body">
        <label className="flex flex-col gap-hair">
          <span className={labelCls}>Price in / Mtok (0 = free)</span>
          <input
            type="number"
            step="0.01"
            min={0}
            className={field}
            value={form.price_in_per_m}
            onChange={(e) => set("price_in_per_m", Number(e.target.value))}
          />
        </label>
        <label className="flex flex-col gap-hair">
          <span className={labelCls}>Price out / Mtok</span>
          <input
            type="number"
            step="0.01"
            min={0}
            className={field}
            value={form.price_out_per_m}
            onChange={(e) => set("price_out_per_m", Number(e.target.value))}
          />
        </label>
      </div>

      {err && (
        <p role="alert" className="font-ui text-[0.8rem] text-unsupported">
          {err instanceof ApiError ? err.message : "Couldn't save the model."}
        </p>
      )}

      <div className="flex justify-end gap-inline">
        <Dialog.Close asChild>
          <button
            type="button"
            className="rounded-control border border-hairline px-body py-hair font-ui text-[0.82rem] text-text-muted hover:text-text"
          >
            Cancel
          </button>
        </Dialog.Close>
        <button
          type="submit"
          disabled={busy}
          className="rounded-control bg-accent px-body py-hair font-ui text-[0.82rem] font-medium text-bg disabled:opacity-60"
        >
          {busy ? "Saving…" : mode === "add" ? "Add model" : "Save changes"}
        </button>
      </div>
    </form>
  );
}

function FormDialog({
  open,
  onOpenChange,
  mode,
  initial,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  mode: "add" | "edit";
  initial: ModelUpsert;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/45" />
        <Dialog.Content className="fixed left-1/2 top-1/2 z-50 flex max-h-[88vh] w-[min(40rem,94vw)] -translate-x-1/2 -translate-y-1/2 flex-col gap-body overflow-y-auto rounded-card border border-hairline bg-bg p-body pmx-rise">
          <Dialog.Title className="font-display text-[1.3rem] text-text">
            {mode === "add" ? "Add a model" : `Edit ${initial.id}`}
          </Dialog.Title>
          <Dialog.Description className="font-ui text-[0.82rem] text-text-muted">
            Saved to the shared config the agent-server routes through — a new model is immediately
            assignable above and callable at runtime.
          </Dialog.Description>
          {/* keyed so the form state resets per open/target */}
          <ModelForm key={`${mode}:${initial.id}`} mode={mode} initial={initial} onDone={() => onOpenChange(false)} />
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export function ModelCatalogue() {
  const { data: models } = useModels();
  const del = useDeleteModel();
  const [dialog, setDialog] = useState<{ mode: "add" | "edit"; initial: ModelUpsert } | null>(null);

  return (
    <section aria-labelledby="catalogue-heading" className="flex flex-col gap-inline">
      <div className="flex items-center justify-between">
        <h3 id="catalogue-heading" className="font-ui text-[0.92rem] font-semibold text-text">
          Catalogue
        </h3>
        <button
          type="button"
          onClick={() => setDialog({ mode: "add", initial: BLANK })}
          className="flex items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.8rem] text-text-muted transition-colors hover:border-hairline-strong hover:text-text"
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
              <div className="font-ui text-[0.86rem] font-medium text-text">{m.label}</div>
              <p className="truncate font-mono text-[0.72rem] text-text-faint">
                {m.id} · {m.note}
              </p>
            </div>
            <div className="flex shrink-0 items-center gap-hair">
              <button
                type="button"
                aria-label={`Edit ${m.id}`}
                onClick={() => setDialog({ mode: "edit", initial: toUpsert(m) })}
                className="rounded-control p-hair text-text-faint transition-colors hover:text-text"
              >
                <Pencil className="size-3.5" aria-hidden />
              </button>
              <button
                type="button"
                aria-label={`Remove ${m.id}`}
                onClick={() => {
                  if (confirm(`Remove ${m.id} from the catalogue?`)) del.mutate(m.id);
                }}
                className="rounded-control p-hair text-text-faint transition-colors hover:text-unsupported"
              >
                <Trash2 className="size-3.5" aria-hidden />
              </button>
            </div>
          </li>
        ))}
      </ul>
      {del.error && (
        <p role="alert" className="font-ui text-[0.8rem] text-unsupported">
          {del.error instanceof ApiError ? del.error.message : "Couldn't remove the model."}
        </p>
      )}

      {dialog && (
        <FormDialog
          open
          onOpenChange={(o) => !o && setDialog(null)}
          mode={dialog.mode}
          initial={dialog.initial}
        />
      )}
    </section>
  );
}
