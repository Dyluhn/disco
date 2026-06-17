import { KeyRound, Lock, Pencil, X } from "lucide-react";
import { useState } from "react";
import { ApiError } from "@/api/client";
import { useClearSecret, useSecrets, useSetSecret } from "@/hooks/useSecrets";

const field =
  "w-full rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.84rem] text-text outline-none focus:border-hairline-strong";

// An env-var-style name (matches the server's validation). Shown inline so the
// user knows WHY a name was rejected before the request round-trips.
const NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

/** One stored key: name + Edit (replace value in place) + Clear (delete). Edit
 * reveals a value input — values are write-only (we never read a key back), so
 * "edit" is really "overwrite with a new value", which is exactly a PUT. */
function StoredKeyRow({
  name,
  onSave,
  onClear,
  saving,
}: {
  name: string;
  onSave: (value: string) => void;
  onClear: () => void;
  saving: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");

  if (editing) {
    return (
      <li className="flex flex-col gap-hair rounded-control border border-hairline bg-surface-1 px-body py-inline">
        <span className="font-mono text-[0.8rem] text-text-muted">{name}</span>
        <div className="flex gap-inline">
          <input
            type="password"
            autoFocus
            className={field}
            value={draft}
            placeholder="new key value"
            aria-label={`New value for ${name}`}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && draft.trim()) {
                onSave(draft.trim());
                setEditing(false);
                setDraft("");
              }
              if (e.key === "Escape") {
                setEditing(false);
                setDraft("");
              }
            }}
          />
          <button
            type="button"
            disabled={saving || !draft.trim()}
            onClick={() => {
              onSave(draft.trim());
              setEditing(false);
              setDraft("");
            }}
            className="shrink-0 rounded-control bg-accent px-body py-hair font-ui text-[0.82rem] font-medium text-bg disabled:opacity-60"
          >
            Save
          </button>
          <button
            type="button"
            onClick={() => {
              setEditing(false);
              setDraft("");
            }}
            aria-label="Cancel edit"
            className="shrink-0 rounded-control border border-hairline px-inline py-hair font-ui text-[0.82rem] text-text-muted hover:text-text"
          >
            <X className="size-3.5" aria-hidden />
          </button>
        </div>
      </li>
    );
  }

  return (
    <li className="flex items-center justify-between gap-inline rounded-control border border-hairline bg-surface-1 px-body py-inline">
      <span className="flex items-center gap-hair font-mono text-[0.82rem] text-text">
        <KeyRound className="size-3.5 text-supported" aria-hidden /> {name}
      </span>
      <div className="flex items-center gap-inline">
        <button
          type="button"
          onClick={() => setEditing(true)}
          className="flex items-center gap-hair font-ui text-[0.8rem] text-text-muted hover:text-text"
        >
          <Pencil className="size-3" aria-hidden /> Edit
        </button>
        <button
          type="button"
          onClick={onClear}
          className="font-ui text-[0.8rem] text-text-muted hover:text-unsupported"
        >
          Clear
        </button>
      </div>
    </li>
  );
}

/**
 * Manage encrypted-at-rest provider API keys by env-var NAME — the generic
 * counterpart to the OpenRouter key field. The user enters the env var their
 * model/search/extraction/TTS provider is configured to read (its `api_key_env`)
 * plus the secret value; the agent-server overlays the decrypted value into that
 * env var at build time, so the plaintext never lands on disk. Full CRUD:
 * add (create), list (read-status; values are write-only), edit (update), clear.
 */
export function ProviderKeysSection() {
  const { data } = useSecrets();
  const setSecret = useSetSecret();
  const clearSecret = useClearSecret();
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);

  // Render the section shell even before/without data (matches OpenRouterSection)
  // — the heading + entry form always show; the stored-list and notices fill in
  // once the query resolves. Never vanish the whole section on load.
  const storedNames = data?.names ?? [];
  const canStore = data?.can_store ?? true;
  const locked = data?.locked ?? false;

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const n = name.trim();
    if (!NAME_RE.test(n)) {
      setNameError("Use the provider's env-var name, e.g. OPENAI_API_KEY or TAVILY_API_KEY.");
      return;
    }
    setNameError(null);
    setSecret.mutate(
      { name: n, value: value.trim() },
      { onSuccess: () => { setName(""); setValue(""); } },
    );
  };

  return (
    <section aria-labelledby="provider-keys-heading" className="flex flex-col gap-inline">
      <h3 id="provider-keys-heading" className="font-ui text-[0.92rem] font-semibold text-text">
        Provider API keys
      </h3>
      <p className="font-ui text-[0.82rem] text-text-muted">
        Store any provider's API key encrypted at rest — paid models, web search, extraction, or
        TTS. Enter the env-var name the provider reads (its <code className="font-mono text-[0.78rem] text-text">api_key_env</code>, set in its section
        above) and the key. It's encrypted with <code className="font-mono text-[0.78rem] text-text">DISCO_SECRET_KEY</code> and the plaintext never touches disk.
      </p>

      {/* Stored-but-undecryptable: DISCO_SECRET_KEY changed/lost since save. Loud,
          because nothing here will actually authenticate until it's restored. */}
      {locked && (
        <div role="alert" className="flex items-start gap-hair rounded-control border border-unsupported/40 bg-unsupported/5 px-body py-inline">
          <Lock className="mt-0.5 size-3.5 shrink-0 text-unsupported" aria-hidden />
          <p className="font-ui text-[0.78rem] leading-snug text-text">
            One or more stored keys can't be decrypted — <code className="font-mono text-[0.76rem]">DISCO_SECRET_KEY</code> is
            missing or different from when they were saved. Restore that app secret, or clear and
            re-enter each key below.
          </p>
        </div>
      )}

      {!canStore && !locked && (
        <div role="note" className="rounded-control border border-hairline bg-surface-1 px-body py-inline">
          <p className="font-ui text-[0.78rem] leading-snug text-text-muted">
            The server doesn't have <code className="font-mono text-[0.76rem] text-text">DISCO_SECRET_KEY</code> set, so keys can't be
            encrypted at rest. Ask the operator to set the app secret to enable storage.
          </p>
        </div>
      )}

      {storedNames.length > 0 && (
        <ul className="flex flex-col gap-hair">
          {storedNames.map((n) => (
            <StoredKeyRow
              key={n}
              name={n}
              saving={setSecret.isPending}
              onSave={(v) => setSecret.mutate({ name: n, value: v })}
              onClear={() => clearSecret.mutate(n)}
            />
          ))}
        </ul>
      )}

      <form onSubmit={submit} className="flex flex-col gap-hair">
        <div className="flex gap-inline">
          <input
            className={`${field} font-mono`}
            value={name}
            placeholder="ENV_VAR_NAME (e.g. OPENAI_API_KEY)"
            onChange={(e) => setName(e.target.value)}
            aria-label="Provider key env-var name"
          />
          <input
            type="password"
            className={field}
            value={value}
            placeholder="key value (encrypted at rest)"
            onChange={(e) => setValue(e.target.value)}
            aria-label="Provider key value"
          />
          <button
            type="submit"
            disabled={setSecret.isPending || !name.trim() || !value.trim()}
            className="shrink-0 rounded-control bg-accent px-body py-hair font-ui text-[0.82rem] font-medium text-bg disabled:opacity-60"
          >
            {setSecret.isPending ? "Saving…" : "Add key"}
          </button>
        </div>
        {nameError && (
          <p role="alert" className="font-ui text-[0.78rem] text-unsupported">{nameError}</p>
        )}
        {setSecret.error && (
          <p role="alert" className="font-ui text-[0.78rem] text-unsupported">
            {setSecret.error instanceof ApiError ? setSecret.error.message : "Couldn't save the key."}
          </p>
        )}
      </form>
    </section>
  );
}
