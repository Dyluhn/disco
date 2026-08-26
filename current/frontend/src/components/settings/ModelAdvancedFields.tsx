import { CAPABILITY_LABEL, type Capability, type ModelUpsert } from "@/types/models";

const CAPS: Capability[] = ["tool_calling", "json_mode", "long_context"];
const field =
  "min-h-11 w-full rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.84rem] text-text outline-none focus:border-hairline-strong lg:min-h-0";
const labelCls =
  "font-ui text-[0.74rem] font-medium uppercase tracking-wide text-text-faint";

export function ModelAdvancedFields({
  form,
  onChange,
}: {
  form: ModelUpsert;
  onChange: <K extends keyof ModelUpsert>(key: K, value: ModelUpsert[K]) => void;
}) {
  return (
    <details className="rounded-control border border-hairline bg-surface-1/30 px-body py-inline">
      <summary className="cursor-pointer py-3 font-ui text-[0.84rem] font-medium text-text lg:py-0">
        Advanced model metadata
      </summary>
      <div className="mt-body flex flex-col gap-body">
        <div className="grid grid-cols-2 gap-body">
          <label className="flex flex-col gap-hair">
            <span className={labelCls}>Context window</span>
            <input
              type="number"
              className={field}
              value={form.context_window}
              min={1}
              onChange={(event) => onChange("context_window", Number(event.target.value))}
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
              onChange={(event) =>
                onChange(
                  "max_output_tokens",
                  event.target.value ? Number(event.target.value) : null,
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
              onChange={(event) => onChange("quantization", event.target.value)}
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
                    onChange(
                      "capabilities",
                      event.target.checked
                        ? [...form.capabilities, capability]
                        : form.capabilities.filter((current) => current !== capability),
                    )
                  }
                />
                {CAPABILITY_LABEL[capability]}
              </label>
            ))}
          </div>
        </fieldset>

        <div className="grid grid-cols-2 gap-body">
          {form.pricing_mode === "subscription" ? (
            <p
              data-disco-flag="model-subscription-no-price"
              className="col-span-2 rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.8rem] text-text-muted"
            >
              Subscription — a flat-rate plan. No per-token price; this model
              shows as “Subscription” everywhere (never “Free”, never a $/Mtok rate).
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
                  onChange={(event) => onChange("price_in_per_m", Number(event.target.value))}
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
                  onChange={(event) => onChange("price_out_per_m", Number(event.target.value))}
                />
              </label>
            </>
          )}
          <label className="col-span-2 flex flex-col gap-hair">
            <span className={labelCls}>Pricing</span>
            <select
              className={field}
              value={form.pricing_mode ?? "metered"}
              onChange={(event) =>
                onChange("pricing_mode", event.target.value as ModelUpsert["pricing_mode"])
              }
            >
              <option value="metered">Metered — pay per token (uses the prices above)</option>
              <option value="subscription">Subscription — flat plan, shown as “Subscription”</option>
              <option value="free">Free — no charge</option>
            </select>
          </label>
        </div>
      </div>
    </details>
  );
}
