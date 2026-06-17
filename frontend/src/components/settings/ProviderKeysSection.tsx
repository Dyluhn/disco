import { AlertTriangle, Check, KeyRound, Lock, Pencil, X } from "lucide-react";
import { useRef, useState } from "react";
import { ApiError } from "@/api/client";
import {
  useClearSecret,
  useExpectedKeyNames,
  useSecrets,
  useSetSecret,
} from "@/hooks/useSecrets";

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
  locked,
  onSave,
  onClear,
  saving,
}: {
  name: string;
  locked: boolean;
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
    <li
      className={`flex items-center justify-between gap-inline rounded-control border px-body py-inline ${
        locked ? "border-unsupported/40 bg-unsupported/5" : "border-hairline bg-surface-1"
      }`}
    >
      <span className="flex items-center gap-hair font-mono text-[0.82rem] text-text">
        {locked ? (
          <Lock className="size-3.5 text-unsupported" aria-hidden />
        ) : (
          <KeyRound className="size-3.5 text-supported" aria-hidden />
        )}
        {name}
        {locked && <span className="font-ui text-[0.74rem] text-unsupported">can't decrypt</span>}
      </span>
      <div className="flex items-center gap-inline">
        <button
          type="button"
          onClick={() => setEditing(true)}
          className="flex items-center gap-hair font-ui text-[0.8rem] text-text-muted hover:text-text"
        >
          <Pencil className="size-3" aria-hidden /> {locked ? "Re-enter" : "Edit"}
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
  const expectedNames = useExpectedKeyNames();
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);
  const valueRef = useRef<HTMLInputElement>(null);

  // Render the section shell even before/without data (matches OpenRouterSection)
  // — the heading + entry form always show; the stored-list and notices fill in
  // once the query resolves. Never vanish the whole section on load.
  const storedNames = data?.names ?? [];
  const stored = new Set(storedNames);
  const canStore = data?.can_store ?? true;
  // The SPECIFIC keys that can't be decrypted — so we name them, not "one or more".
  const lockedNames = data?.locked_names ?? [];
  const lockedSet = new Set(lockedNames);
  // Keys the configured providers reference but that AREN'T stored yet — the
  // running app will look for these and fail to find them.
  const missing = expectedNames.filter((n) => !stored.has(n));

  // Prefill the add form with a referenced env-var name and focus the value box.
  const prefill = (envName: string) => {
    setName(envName);
    setNameError(null);
    valueRef.current?.focus();
  };

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
          and SPECIFIC — names exactly which keys need restoring/re-entering. */}
      {lockedNames.length > 0 && (
        <div role="alert" className="flex items-start gap-hair rounded-control border border-unsupported/40 bg-unsupported/5 px-body py-inline">
          <Lock className="mt-0.5 size-3.5 shrink-0 text-unsupported" aria-hidden />
          <p className="font-ui text-[0.78rem] leading-snug text-text">
            {lockedNames.length === 1 ? "This key can't" : "These keys can't"} be decrypted —{" "}
            {lockedNames.map((n, i) => (
              <span key={n}>
                {i > 0 && ", "}
                <code className="font-mono text-[0.76rem] text-unsupported">{n}</code>
              </span>
            ))}
            . <code className="font-mono text-[0.76rem]">DISCO_SECRET_KEY</code> is missing or
            different from when {lockedNames.length === 1 ? "it was" : "they were"} saved. Restore
            that app secret, or Edit each one below to re-enter its value.
          </p>
        </div>
      )}

      {!canStore && lockedNames.length === 0 && (
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
              locked={lockedSet.has(n)}
              saving={setSecret.isPending}
              onSave={(v) => setSecret.mutate({ name: n, value: v })}
              onClear={() => clearSecret.mutate(n)}
            />
          ))}
        </ul>
      )}

      {/* Cross-reference: env vars the configured providers reference, with
          stored/missing status. The missing ones are what the running app will
          fail to find — prefill the form to fix them in one click. */}
      {expectedNames.length > 0 && (
        <div className="flex flex-col gap-hair">
          <span className="font-ui text-[0.78rem] text-text-muted">
            Referenced by your providers
            {missing.length > 0 && (
              <span className="text-unsupported"> · {missing.length} not stored yet</span>
            )}
          </span>
          <ul className="flex flex-col gap-hair">
            {expectedNames.map((n) => {
              const isStored = stored.has(n);
              return (
                <li
                  key={n}
                  className="flex items-center justify-between gap-inline rounded-control border border-hairline bg-bg px-body py-hair"
                >
                  <span className="flex items-center gap-hair font-mono text-[0.8rem] text-text">
                    {isStored ? (
                      <Check className="size-3.5 text-supported" aria-hidden />
                    ) : (
                      <AlertTriangle className="size-3.5 text-unsupported" aria-hidden />
                    )}
                    {n}
                  </span>
                  {isStored ? (
                    <span className="font-ui text-[0.76rem] text-supported">stored</span>
                  ) : (
                    <button
                      type="button"
                      onClick={() => prefill(n)}
                      className="font-ui text-[0.78rem] text-accent hover:underline"
                    >
                      Add key
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
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
            ref={valueRef}
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
