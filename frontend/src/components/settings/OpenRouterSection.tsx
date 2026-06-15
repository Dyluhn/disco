import * as Dialog from "@radix-ui/react-dialog";
import { Check, KeyRound, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { ApiError } from "@/api/client";
import { openRouterUpsert } from "@/api/models";
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
import { NotWired } from "./NotWired";

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

  // Can't store at all: no app secret to encrypt with.
  if (!status.can_store && !status.configured) {
    return (
      <NotWired detail="Encrypted key storage needs an app secret: set DISCO_SECRET_KEY on the servers, then you can save an OpenRouter key here (it's encrypted at rest, never plaintext on disk)." />
    );
  }
  // Stored but can't be decrypted (app secret missing/wrong since it was saved).
  if (status.locked) {
    return (
      <NotWired detail="An OpenRouter key is stored but can't be decrypted — DISCO_SECRET_KEY is missing or different from when it was saved. Restore that app secret, or clear and re-enter the key." />
    );
  }
  if (status.configured) {
    return (
      <div className="flex items-center justify-between gap-inline rounded-control border border-hairline bg-surface-1 px-body py-inline">
        <span className="flex items-center gap-hair font-ui text-[0.84rem] text-text">
          <KeyRound className="size-3.5 text-supported" aria-hidden /> Key configured (encrypted at
          rest)
        </span>
        <button
          type="button"
          onClick={() => clearKey.mutate()}
          className="font-ui text-[0.8rem] text-text-muted hover:text-unsupported"
        >
          Clear
        </button>
      </div>
    );
  }
  // Not configured yet, but storable.
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (value.trim()) setKey.mutate(value.trim(), { onSuccess: () => setValue("") });
      }}
      className="flex flex-col gap-hair"
    >
      <div className="flex gap-inline">
        <input
          type="password"
          className={field}
          value={value}
          placeholder="sk-or-v1-…  (encrypted at rest with DISCO_SECRET_KEY)"
          onChange={(e) => setValue(e.target.value)}
          aria-label="OpenRouter API key"
        />
        <button
          type="submit"
          disabled={setKey.isPending || !value.trim()}
          className="shrink-0 rounded-control bg-accent px-body py-hair font-ui text-[0.82rem] font-medium text-bg disabled:opacity-60"
        >
          {setKey.isPending ? "Saving…" : "Save key"}
        </button>
      </div>
      {setKey.error && (
        <p role="alert" className="font-ui text-[0.78rem] text-unsupported">
          {setKey.error instanceof ApiError ? setKey.error.message : "Couldn't save the key."}
        </p>
      )}
    </form>
  );
}

/** Browse the live OpenRouter catalogue and add a model to ours. */
function BrowseDialog({ addedSlugs }: { addedSlugs: Set<string> }) {
  const [open, setOpen] = useState(false);
  const { data: models, isLoading, isError } = useOpenRouterModels(open);
  const create = useCreateModel();
  const [q, setQ] = useState("");

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const all = models ?? [];
    const matched = needle
      ? all.filter((m) => m.id.toLowerCase().includes(needle) || m.name.toLowerCase().includes(needle))
      : all;
    return matched;
  }, [models, q]);

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Trigger asChild>
        <button
          type="button"
          className="flex items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.8rem] text-text-muted transition-colors hover:border-hairline-strong hover:text-text"
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
              Adding one creates a catalogue entry pointing at OpenRouter with your shared key — then
              it's assignable like any other model.
            </Dialog.Description>
            <input
              autoFocus
              className={cn(field, "mt-inline")}
              value={q}
              placeholder="Search 300+ models — vendor or name…"
              onChange={(e) => setQ(e.target.value)}
            />
          </div>
          <div className="flex-1 overflow-y-auto">
            {isLoading && (
              <p className="px-body py-body font-ui text-[0.84rem] text-text-muted">Loading…</p>
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
                    <div className="min-w-0">
                      <div className="font-ui text-[0.86rem] text-text">{m.name}</div>
                      <p className="truncate font-mono text-[0.72rem] text-text-faint">
                        {m.id} · {Math.floor(m.context_length / 1000)}K ctx · {price(m)}
                      </p>
                    </div>
                    <button
                      type="button"
                      disabled={added || create.isPending}
                      onClick={() => create.mutate(openRouterUpsert(m))}
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

  return (
    <section aria-labelledby="openrouter-heading" className="flex flex-col gap-inline">
      <div className="flex items-center justify-between">
        <h3 id="openrouter-heading" className="font-ui text-[0.92rem] font-semibold text-text">
          OpenRouter
        </h3>
        <BrowseDialog addedSlugs={addedSlugs} />
      </div>
      <p className="font-ui text-[0.82rem] text-text-muted">
        One API key, any model hosted on OpenRouter. The key is encrypted at rest; added models share
        it and the OpenRouter endpoint.
      </p>
      <KeyManager />
    </section>
  );
}
