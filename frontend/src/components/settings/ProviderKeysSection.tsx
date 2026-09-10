import { AlertTriangle, Check, KeyRound, Lock, Pencil, X } from "lucide-react";
import { useRef, useState } from "react";
import { isApiFailure } from "@/api/errors";
import { apiIsLive } from "@/api/liveness";
import { testSecret } from "@/api/secrets";
import {
  useClearSecret,
  useExpectedKeyNames,
  useSecrets,
  useSetSecret,
} from "@/hooks/useSecrets";
import { ProbeButton } from "./ProbeButton";

const field =
  "min-h-11 w-full rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.84rem] text-text outline-none focus:border-hairline-strong lg:min-h-0";

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
            data-disco-control="settings.provider-key-save"
            disabled={saving || !draft.trim()}
            onClick={() => {
              onSave(draft.trim());
              setEditing(false);
              setDraft("");
            }}
            className="min-h-11 shrink-0 rounded-control bg-accent px-body py-hair font-ui text-[0.82rem] font-medium text-bg disabled:opacity-60 lg:min-h-0"
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
            className="inline-flex min-h-11 min-w-11 shrink-0 items-center justify-center rounded-control border border-hairline px-inline py-hair font-ui text-[0.82rem] text-text-muted hover:text-text lg:min-h-0 lg:min-w-0"
          >
            <X className="size-3.5" aria-hidden />
          </button>
        </div>
      </li>
    );
  }

  return (
    <li
      className={`flex flex-col gap-hair rounded-control border px-body py-inline ${
        locked
          ? "border-unsupported/40 bg-unsupported/5"
          : "border-hairline bg-surface-1"
      }`}
    >
      <div className="flex items-center justify-between gap-inline">
        <span className="flex items-center gap-hair font-mono text-[0.82rem] text-text">
          {locked ? (
            <Lock className="size-3.5 text-unsupported" aria-hidden />
          ) : (
            <KeyRound className="size-3.5 text-supported" aria-hidden />
          )}
          {name}
          {locked && (
            <span className="font-ui text-[0.74rem] text-unsupported">
              can't decrypt
            </span>
          )}
        </span>
        <div className="flex items-center gap-inline">
          <button
            type="button"
            onClick={() => setEditing(true)}
            className="flex min-h-11 items-center gap-hair font-ui text-[0.8rem] text-text-muted hover:text-text lg:min-h-0"
          >
            <Pencil className="size-3" aria-hidden />{" "}
            {locked ? "Re-enter" : "Edit"}
          </button>
          <button
            type="button"
            data-disco-control="settings.provider-key-clear"
            onClick={onClear}
            className="min-h-11 font-ui text-[0.8rem] text-text-muted hover:text-unsupported lg:min-h-0"
          >
            Clear
          </button>
        </div>
      </div>
      {/* T4.1 live probe: a real authenticated call to the provider that uses
          this key. Disabled when the key can't be decrypted or no backend is
          connected (no false affordance). */}
      {!locked && (
        <ProbeButton
          control="settings.provider-key-test"
          idleLabel="Test key"
          run={() => testSecret(name)}
          disabled={!apiIsLive()}
          disabledHint="connect a backend to test"
        />
      )}
    </li>
  );
}

/**
 * Advanced provider keys by config name — the generic counterpart to the
 * first-class OpenRouter key field. Some local/self-hosted and paid services
 * still reference a named key in their config; the agent-server overlays the
 * decrypted value at build time, so the plaintext never lands on disk. Full CRUD:
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
  const storedNames = [...new Set(data?.names ?? [])];
  const stored = new Set(storedNames);
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
      setNameError(
        "Use a provider key name, e.g. OPENAI_API_KEY or TAVILY_API_KEY.",
      );
      return;
    }
    setNameError(null);
    setSecret.mutate(
      { name: n, value: value.trim() },
      {
        onSuccess: () => {
          setName("");
          setValue("");
        },
      },
    );
  };

  return (
    <section
      aria-labelledby="provider-keys-heading"
      className="flex flex-col gap-inline"
    >
      <h4
        id="provider-keys-heading"
        className="font-ui text-[0.88rem] font-semibold text-text"
      >
        Provider API keys
      </h4>
      <p className="font-ui text-[0.82rem] text-text-muted">
        Advanced key storage for providers that still reference a named key in
        their configuration: paid models, web search, extraction, TTS, or image
        generation. Values are encrypted at rest and never shown back.
      </p>

      {/* Stored-but-undecryptable: app key changed/lost since save. Loud, specific,
          and action-oriented — name exactly which keys need re-entering. */}
      {lockedNames.length > 0 && (
        <div
          role="alert"
          className="flex items-start gap-hair rounded-control border border-warn/50 bg-warn/5 px-body py-inline"
        >
          <Lock className="mt-0.5 size-3.5 shrink-0 text-warn" aria-hidden />
          <p className="font-ui text-[0.78rem] leading-snug text-text">
            Stored {lockedNames.length === 1 ? "key can't" : "keys can't"} be
            decrypted — re-enter {lockedNames.length === 1 ? "it" : "them"}:{" "}
            {lockedNames.map((n, i) => (
              <span key={n}>
                {i > 0 && ", "}
                <code className="font-mono text-[0.76rem] text-warn">{n}</code>
              </span>
            ))}
            .
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
              <span className="text-unsupported">
                {" "}
                · {missing.length} not stored yet
              </span>
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
                      <AlertTriangle
                        className="size-3.5 text-unsupported"
                        aria-hidden
                      />
                    )}
                    {n}
                  </span>
                  {isStored ? (
                    <span className="font-ui text-[0.76rem] text-supported">
                      stored
                    </span>
                  ) : (
                    <button
                      type="button"
                      onClick={() => prefill(n)}
                      className="min-h-11 font-ui text-[0.78rem] text-accent hover:underline lg:min-h-0"
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
            placeholder="KEY_NAME (e.g. OPENAI_API_KEY)"
            onChange={(e) => setName(e.target.value)}
            aria-label="Provider key name"
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
            data-disco-control="settings.provider-key-add"
            disabled={setSecret.isPending || !name.trim() || !value.trim()}
            className="min-h-11 shrink-0 rounded-control bg-accent px-body py-hair font-ui text-[0.82rem] font-medium text-bg disabled:opacity-60 lg:min-h-0"
          >
            {setSecret.isPending ? "Saving…" : "Add key"}
          </button>
        </div>
        {/* Live validity: each stored key has a real "Test key" probe (above)
            that makes an authenticated call to the provider that uses it. A green
            means the provider really answered; a failure is shown honestly. */}
        <p
          data-key-validity="tested-on-demand"
          className="font-ui text-[0.74rem] text-text-faint"
        >
          Stored keys are encrypted at rest. Use “Test key” on a stored key to
          verify it with a real authenticated call to its provider — a key
          referenced by a configured model can be checked without spending a
          conversation.
        </p>
        {nameError && (
          <p role="alert" className="font-ui text-[0.78rem] text-unsupported">
            {nameError}
          </p>
        )}
        {setSecret.error && (
          <p role="alert" className="font-ui text-[0.78rem] text-unsupported">
            {isApiFailure(setSecret.error)
              ? setSecret.error.message
              : "Couldn't save the key."}
          </p>
        )}
      </form>
    </section>
  );
}
