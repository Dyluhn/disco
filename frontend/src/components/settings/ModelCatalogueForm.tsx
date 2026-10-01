import * as Dialog from "@radix-ui/react-dialog";
import { type FormEvent, useState } from "react";
import { isApiFailure } from "@/api/errors";
import { cn } from "@/lib/cn";
import { useCreateModel, useUpdateModel } from "@/hooks/useModels";
import {
  type Capability,
  CAPABILITY_LABEL,
  type ModelInfo,
  type ModelUpsert,
} from "@/types/models";
import { RequestPolicyEditor } from "./RequestPolicyEditor";
import { modelUpsertPayload, useRequestPolicyForm } from "./requestPolicyForm";
import { StoredCredentialField } from "./StoredCredentialField";

const CAPS: Capability[] = [
  "tool_calling",
  "json_mode",
  "long_context",
];

export const BLANK: ModelUpsert = {
  id: "",
  model_id: "",
  base_url: "",
  api_key_env: "",
  context_window: 8192,
  max_output_tokens: null,
  quantization: "",
  capabilities: [],
  vision: null,
  price_in_per_m: 0,
  price_out_per_m: 0,
  pricing_mode: "metered",
};

export function toUpsert(m: ModelInfo): ModelUpsert {
  return {
    id: m.id,
    request_policy: m.request_policy,
    model_id: m.model_id,
    base_url: m.base_url ?? "",
    api_key_env: m.api_key_env ?? "",
    context_window: m.context_window,
    max_output_tokens: m.max_output_tokens ?? null,
    quantization: m.quantization ?? "",
    capabilities: m.capabilities.filter((capability) => capability !== "vision"),
    vision: m.vision ?? null,
    requires_api_key: m.requires_api_key ?? true,
    price_in_per_m: m.price_in_per_m,
    price_out_per_m: m.price_out_per_m,
    // W-05: preserve the pay model on edit; derive a sensible default when unset.
    pricing_mode:
      m.pricing_mode ??
      (m.price_in_per_m > 0 || m.price_out_per_m > 0 ? "metered" : "free"),
  };
}

const field =
  "min-h-11 w-full rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.84rem] text-text outline-none focus:border-hairline-strong lg:min-h-0";
const labelCls =
  "font-ui text-[0.74rem] font-medium uppercase tracking-wide text-text-faint";

function visionSelectValue(value: boolean | null | undefined): string {
  if (value === null || value === undefined) return "auto";
  return String(value);
}

function visionFromSelect(value: string): boolean | null {
  if (value === "auto") return null;
  return value === "true";
}

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
  const policyEditor = useRequestPolicyForm(initial.request_policy);
  const create = useCreateModel();
  const update = useUpdateModel();
  const busy = create.isPending || update.isPending;
  const err = (create.error ?? update.error) as Error | null;
  const set = <K extends keyof ModelUpsert>(k: K, v: ModelUpsert[K]) =>
    setForm((f) => ({ ...f, [k]: v }));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const policy = policyEditor.parse();
    if (!policy) return;
    const payload = modelUpsertPayload(form, policy);
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
        <StoredCredentialField
          label="Model credential"
          value={form.api_key_env ?? ""}
          onChange={(value) => set("api_key_env", value)}
          placeholder="OPENAI_API_KEY"
          className="col-span-2"
          inputClassName={field}
        />
      </div>

      <details className="rounded-control border border-hairline bg-surface-1/30 px-body py-inline">
        <summary className="cursor-pointer py-3 font-ui text-[0.84rem] font-medium text-text lg:py-0">
          Advanced model metadata
        </summary>
        <div className="mt-body flex flex-col gap-body">
          <RequestPolicyEditor editor={policyEditor} />
          <div className="grid grid-cols-2 gap-body">
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
              <span className={labelCls}>Maximum output tokens (optional)</span>
              <input
                type="number"
                className={field}
                value={form.max_output_tokens ?? ""}
                min={1}
                placeholder="Provider default"
                onChange={(e) =>
                  set(
                    "max_output_tokens",
                    e.target.value ? Number(e.target.value) : null,
                  )
                }
              />
            </label>
            <label className="col-span-2 flex flex-col gap-hair">
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
              {CAPS.map((capability) => (
                <label
                  key={capability}
                  className="flex items-center gap-hair font-ui text-[0.8rem] text-text-muted"
                >
                  <input
                    type="checkbox"
                    checked={form.capabilities.includes(capability)}
                    onChange={(event) =>
                      set(
                        "capabilities",
                        event.target.checked
                          ? [...form.capabilities, capability]
                          : form.capabilities.filter(
                              (current) => current !== capability,
                            ),
                      )
                    }
                  />
                  {CAPABILITY_LABEL[capability]}
                </label>
              ))}
            </div>
          </fieldset>

          <label className="flex flex-col gap-hair">
            <span className={labelCls}>Image understanding</span>
            <select
              className={field}
              value={visionSelectValue(form.vision)}
              onChange={(e) => set("vision", visionFromSelect(e.target.value))}
            >
              <option value="auto">Auto-detect from the provider</option>
              <option value="true">Supports images</option>
              <option value="false">Text only</option>
            </select>
            <span className="font-ui text-[0.76rem] leading-snug text-text-faint">
              Override detection only when you know the endpoint's actual image
              capability. This controls whether pixels may be sent to the model.
            </span>
          </label>

          <div className="grid grid-cols-2 gap-body">
            {form.pricing_mode === "subscription" ? (
              <p
                data-disco-flag="model-subscription-no-price"
                className="col-span-2 rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.8rem] text-text-muted"
              >
                Subscription — a flat-rate plan. No per-token price; this model
                shows as “Subscription” everywhere (never “Free”, never a $/Mtok
                rate).
              </p>
            ) : (
              <>
                <label className="flex flex-col gap-hair">
                  <span className={labelCls}>Price in / Mtok (0 = free)</span>
                  <input
                    type="number"
                    step="0.01"
                    min={0}
                    className={field}
                    value={form.price_in_per_m}
                    onChange={(e) =>
                      set("price_in_per_m", Number(e.target.value))
                    }
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
                    onChange={(e) =>
                      set("price_out_per_m", Number(e.target.value))
                    }
                  />
                </label>
              </>
            )}
            <label className="col-span-2 flex flex-col gap-hair">
              <span className={labelCls}>Pricing</span>
              <select
                className={field}
                value={form.pricing_mode ?? "metered"}
                onChange={(e) =>
                  set(
                    "pricing_mode",
                    e.target.value as ModelUpsert["pricing_mode"],
                  )
                }
              >
                <option value="metered">
                  Metered — pay per token (uses the prices above)
                </option>
                <option value="subscription">
                  Subscription — flat plan, shown as “Subscription”
                </option>
                <option value="free">Free — no charge</option>
              </select>
            </label>
          </div>
        </div>
      </details>

      {err && (
        <p role="alert" className="font-ui text-[0.8rem] text-unsupported">
          {isApiFailure(err) ? err.message : "Couldn't save the model."}
        </p>
      )}

      <div className="flex justify-end gap-inline">
        <Dialog.Close asChild>
          <button
            type="button"
            className="min-h-11 rounded-control border border-hairline px-body py-hair font-ui text-[0.82rem] text-text-muted hover:text-text lg:min-h-0"
          >
            Cancel
          </button>
        </Dialog.Close>
        <button
          type="submit"
          disabled={busy}
          className="min-h-11 rounded-control bg-accent px-body py-hair font-ui text-[0.82rem] font-medium text-bg disabled:opacity-60 lg:min-h-0"
        >
          {busy ? "Saving…" : mode === "add" ? "Add model" : "Save changes"}
        </button>
      </div>
    </form>
  );
}

export function FormDialog({
  open,
  onOpenChange,
  mode,
  initial,
  trigger,
  addButtonRef,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  mode: "add" | "edit";
  initial: ModelUpsert;
  trigger: HTMLButtonElement | null;
  addButtonRef: { current: HTMLButtonElement | null };
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/45" />
        <Dialog.Content
          className="fixed left-1/2 top-1/2 z-50 flex max-h-[88vh] w-[min(40rem,94vw)] -translate-x-1/2 -translate-y-1/2 flex-col gap-body overflow-y-auto rounded-card border border-hairline bg-bg p-body pmx-rise"
          onCloseAutoFocus={(event) => {
            const live = trigger && trigger.isConnected ? trigger : null;
            const fallback =
              addButtonRef.current && addButtonRef.current.isConnected
                ? addButtonRef.current
                : null;
            const target = live ?? fallback;
            if (target) {
              event.preventDefault();
              target.focus();
            }
          }}
        >
          <Dialog.Title className="font-display text-[1.3rem] text-text">
            {mode === "add" ? "Add a model" : `Edit ${initial.id}`}
          </Dialog.Title>
          <Dialog.Description className="font-ui text-[0.82rem] text-text-muted">
            Saved to the shared config the agent-server routes through — a new
            model is immediately assignable above and callable at runtime.
          </Dialog.Description>
          {/* keyed so the form state resets per open/target */}
          <ModelForm
            key={`${mode}:${initial.id}`}
            mode={mode}
            initial={initial}
            onDone={() => onOpenChange(false)}
          />
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
