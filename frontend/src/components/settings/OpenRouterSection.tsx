import * as Dialog from "@radix-ui/react-dialog";
import { Check, KeyRound, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { ApiError, isLive } from "@/api/client";
import { openRouterUpsert } from "@/api/models";
import { testSecret } from "@/api/secrets";
import { cn } from "@/lib/cn";
import {
  useClearOpenRouterKey,
  useCreateModel,
  useModels,
  useOpenRouterKey,
  useOpenRouterModels,
  useSetOpenRouterKey,
} from "@/hooks/useModels";
import type { OpenRouterModel } from "@/types/models";
import { ProbeButton } from "./ProbeButton";

const field =
  "w-full rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.84rem] text-text outline-none focus:border-hairline-strong";
const price = (m: OpenRouterModel) =>
  m.price_in_per_m === 0 && m.price_out_per_m === 0
    ? "Free"
    : `$${m.price_in_per_m.toFixed(2)} / $${m.price_out_per_m.toFixed(2)} / Mtok`;

/** Set / clear the encrypted OpenRouter key. */
function KeyManager() {
  const { data: status } = useOpenRouterKey();
  const setKey = useSetOpenRouterKey();
  const clearKey = useClearOpenRouterKey();
  const [value, setValue] = useState("");

  if (!status) return null;

  const entryForm = (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (value.trim())
          setKey.mutate(value.trim(), { onSuccess: () => setValue("") });
      }}
      className="flex flex-col gap-hair"
    >
      <div className="flex gap-inline">
        <input
          type="password"
          className={field}
          value={value}
          placeholder="sk-or-v1-..."
          onChange={(e) => setValue(e.target.value)}
          aria-label="OpenRouter API key"
        />
        <button
          type="submit"
          data-disco-control="settings.openrouter-key-save"
          disabled={setKey.isPending || !value.trim()}
          className="shrink-0 rounded-control bg-accent px-body py-hair font-ui text-[0.82rem] font-medium text-bg disabled:opacity-60"
        >
          {setKey.isPending ? "Saving…" : "Save key"}
        </button>
      </div>
      {setKey.error && (
        <p role="alert" className="font-ui text-[0.78rem] text-unsupported">
          {setKey.error instanceof ApiError
            ? setKey.error.message
            : "Couldn't save the key."}
        </p>
      )}
    </form>
  );
  if (status.locked) {
    return (
      <div className="flex flex-col gap-inline">
        <div
          role="alert"
          className="rounded-control border border-warn/50 bg-warn/5 px-body py-inline"
        >
          <p className="font-ui text-[0.8rem] leading-snug text-text">
            Stored key can't be decrypted — re-enter it.
          </p>
        </div>
        {entryForm}
        <button
          type="button"
          data-disco-control="settings.openrouter-key-clear"
          onClick={() => clearKey.mutate()}
          className="self-start font-ui text-[0.8rem] text-text-muted hover:text-unsupported"
        >
          Clear stored key
        </button>
      </div>
    );
  }
  if (status.configured) {
    return (
      <div className="flex flex-col gap-inline rounded-control border border-hairline bg-surface-1 px-body py-inline">
        <div className="flex items-center justify-between gap-inline">
          <span className="flex items-center gap-hair font-ui text-[0.84rem] text-text">
            <KeyRound className="size-3.5 text-supported" aria-hidden /> Key
            configured
          </span>
          <button
            type="button"
            data-disco-control="settings.openrouter-key-clear"
            onClick={() => clearKey.mutate()}
            className="font-ui text-[0.8rem] text-text-muted hover:text-unsupported"
          >
            Clear
          </button>
        </div>
        <ProbeButton
          control="settings.openrouter-key-test"
          idleLabel="Test key"
          run={() => testSecret("DISCO_OPENROUTER_API_KEY")}
          disabled={!isLive()}
          disabledHint="connect a backend to test"
        />
      </div>
    );
  }
  return entryForm;
}

/** Browse the live OpenRouter catalogue and add a model to ours. Gated on a usable
 * key: browsing hits the live OpenRouter API, so with no decryptable key the call
 * can only fail — we disable the trigger and say so rather than offer a dead button. */
function BrowseDialog({
  addedSlugs,
  keyReady,
}: {
  addedSlugs: Set<string>;
  keyReady: boolean;
}) {
  const [open, setOpen] = useState(false);
  const { data: models, isLoading, isError } = useOpenRouterModels(open);
  const create = useCreateModel();
  const [q, setQ] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const all = models ?? [];
    const matched = needle
      ? all.filter(
          (m) =>
            m.id.toLowerCase().includes(needle) ||
            m.name.toLowerCase().includes(needle),
        )
      : all;
    return matched;
  }, [models, q]);
  const selectedModels = useMemo(
    () =>
      (models ?? []).filter((m) => selected.has(m.id) && !addedSlugs.has(m.id)),
    [addedSlugs, models, selected],
  );
  const toggleSelected = (id: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };
  const addSelected = async () => {
    for (const model of selectedModels) {
      await create.mutateAsync(openRouterUpsert(model));
    }
    setSelected(new Set());
  };

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Trigger asChild>
        <button
          type="button"
          data-disco-control="settings.openrouter-browse"
          data-key-ready={keyReady}
          disabled={!keyReady}
          title={
            keyReady
              ? undefined
              : "Add a decryptable OpenRouter key first — browsing queries the live API."
          }
          className="flex items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.8rem] text-text-muted transition-colors hover:border-hairline-strong hover:text-text disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Search className="size-3.5" aria-hidden /> Browse OpenRouter models
        </button>
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/45" />
        <Dialog.Content className="fixed left-1/2 top-1/2 z-50 flex max-h-[86vh] w-[min(44rem,94vw)] -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden rounded-card border border-hairline bg-bg pmx-rise">
          <div className="border-b border-hairline px-body py-inline">
            <Dialog.Title className="font-display text-[1.3rem] text-text">
              OpenRouter models
            </Dialog.Title>
            <Dialog.Description className="font-ui text-[0.8rem] text-text-muted">
              Adding one creates a catalogue entry pointing at OpenRouter with
              your shared key — then it's assignable like any other model.
            </Dialog.Description>
            <input
              autoFocus
              data-disco-control="settings.openrouter-search"
              aria-label="Search OpenRouter models"
              className={cn(field, "mt-inline")}
              value={q}
              placeholder="Search 300+ models — vendor or name…"
              onChange={(e) => setQ(e.target.value)}
            />
            <div className="mt-inline flex items-center justify-between gap-inline">
              <span className="font-ui text-[0.76rem] text-text-faint">
                {selectedModels.length} selected
              </span>
              <button
                type="button"
                data-disco-control="settings.openrouter-add-selected"
                disabled={selectedModels.length === 0 || create.isPending}
                onClick={() => void addSelected()}
                className="rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:border-hairline-strong hover:text-text disabled:opacity-50"
              >
                {create.isPending ? "Adding..." : "Add selected"}
              </button>
            </div>
          </div>
          <div className="flex-1 overflow-y-auto">
            {isLoading && (
              <p className="px-body py-body font-ui text-[0.84rem] text-text-muted">
                Loading…
              </p>
            )}
            {isError && (
              <p className="px-body py-body font-ui text-[0.84rem] text-unsupported">
                Couldn't reach OpenRouter.
              </p>
            )}
            {!isLoading &&
              filtered.slice(0, 100).map((m) => {
                const added = addedSlugs.has(m.id);
                return (
                  <div
                    key={m.id}
                    className="flex items-center justify-between gap-section border-b border-hairline px-body py-inline last:border-b-0"
                  >
                    <div className="flex min-w-0 items-start gap-inline">
                      <input
                        type="checkbox"
                        aria-label={`Select ${m.name}`}
                        checked={selected.has(m.id)}
                        disabled={added}
                        onChange={() => toggleSelected(m.id)}
                        className="mt-1"
                      />
                      <div className="min-w-0">
                        <div className="font-ui text-[0.86rem] text-text">
                          {m.name}
                        </div>
                        <p className="truncate font-mono text-[0.72rem] text-text-faint">
                          {m.id} · {Math.floor(m.context_length / 1000)}K ctx ·{" "}
                          {price(m)}
                        </p>
                      </div>
                    </div>
                    <button
                      type="button"
                      data-disco-control="settings.openrouter-add-model"
                      data-model-id={m.id}
                      disabled={added || create.isPending}
                      onClick={() =>
                        create.mutate(openRouterUpsert(m), {
                          onSuccess: () =>
                            setSelected((current) => {
                              const next = new Set(current);
                              next.delete(m.id);
                              return next;
                            }),
                        })
                      }
                      className={cn(
                        "flex shrink-0 items-center gap-hair rounded-control border px-inline py-hair font-ui text-[0.78rem] transition-colors",
                        added
                          ? "border-hairline text-text-faint"
                          : "border-hairline text-text-muted hover:border-hairline-strong hover:text-text",
                      )}
                    >
                      {added ? (
                        <>
                          <Check className="size-3" aria-hidden /> Added
                        </>
                      ) : (
                        "Add"
                      )}
                    </button>
                  </div>
                );
              })}
            {!isLoading && filtered.length > 100 && (
              <p className="px-body py-inline font-ui text-[0.76rem] text-text-faint">
                Showing the first 100 of {filtered.length} — refine your search.
              </p>
            )}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export function OpenRouterSection() {
  const { data: models } = useModels();
  const { data: keyStatus } = useOpenRouterKey();
  // Browsing the live catalogue needs a key that's actually usable (stored AND
  // decryptable). Otherwise the trigger is honestly disabled (no dead affordance).
  const keyReady = Boolean(keyStatus?.configured && !keyStatus?.locked);
  // catalogue entries that came from OpenRouter (so the browser shows "Added")
  const addedSlugs = useMemo(
    () =>
      new Set(
        (models ?? [])
          .filter((m) => (m.base_url ?? "").includes("openrouter.ai"))
          .map((m) => m.model_id),
      ),
    [models],
  );
  const addedModels = useMemo(
    () =>
      (models ?? []).filter((m) =>
        (m.base_url ?? "").includes("openrouter.ai"),
      ),
    [models],
  );

  return (
    <section
      aria-labelledby="openrouter-heading"
      className="flex flex-col gap-inline rounded-card border border-hairline bg-surface-1/50 p-body"
    >
      <div className="flex flex-wrap items-center justify-between gap-inline">
        <h4
          id="openrouter-heading"
          className="font-ui text-[0.9rem] font-semibold text-text"
        >
          OpenRouter
        </h4>
        <BrowseDialog addedSlugs={addedSlugs} keyReady={keyReady} />
      </div>
      <p className="font-ui text-[0.82rem] text-text-muted">
        One API key, any model hosted on OpenRouter. Added models share this key
        and appear in the catalogue, role assignments, and the main-screen model
        picker.
      </p>
      {!keyReady && (
        <p
          role="note"
          data-disco-flag="openrouter-key-required"
          className="font-ui text-[0.78rem] text-warn"
        >
          Add a decryptable OpenRouter key to browse the catalogue and use
          OpenRouter models.
        </p>
      )}
      <KeyManager />
      <div className="flex flex-col gap-hair">
        <span className="font-ui text-[0.76rem] font-medium uppercase text-text-faint">
          Models using this provider
        </span>
        {addedModels.length === 0 ? (
          <p className="rounded-control border border-dashed border-hairline px-body py-inline font-ui text-[0.8rem] text-text-faint">
            No OpenRouter models added yet.
          </p>
        ) : (
          <ul className="overflow-hidden rounded-control border border-hairline bg-bg">
            {addedModels.map((m) => (
              <li
                key={m.id}
                className="flex flex-col gap-hair border-b border-hairline px-body py-hair last:border-b-0 sm:flex-row sm:items-center sm:justify-between sm:gap-inline"
              >
                <span className="min-w-0 truncate font-ui text-[0.82rem] text-text">
                  {m.label}
                </span>
                <span className="break-all font-mono text-[0.7rem] text-text-faint sm:text-right">
                  {m.model_id}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
