import { KeyRound } from "lucide-react";
import { useState } from "react";
import { ApiError } from "@/api/client";
import { useClearSecret, useSecrets, useSetSecret } from "@/hooks/useSecrets";

const field =
  "w-full rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.84rem] text-text outline-none focus:border-hairline-strong";

// An env-var-style name (matches the server's validation). Shown inline so the
// user knows WHY a name was rejected before the request round-trips.
const NAME_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

/**
 * Manage encrypted-at-rest provider API keys by env-var NAME — the generic
 * counterpart to the OpenRouter key field. The user enters the env var their
 * model/search/extraction/TTS provider is configured to read (its `api_key_env`)
 * plus the secret value; the agent-server overlays the decrypted value into that
 * env var at build time, so the plaintext never lands on disk.
 */
export function ProviderKeysSection() {
  const { data } = useSecrets();
  const setSecret = useSetSecret();
  const clearSecret = useClearSecret();
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [nameError, setNameError] = useState<string | null>(null);

  // Render the section shell even before/without data (matches OpenRouterSection)
  // — the heading + entry form always show; the stored-list and can-store note
  // fill in once the query resolves. Never vanish the whole section on load.
  const storedNames = data?.names ?? [];
  const canStore = data?.can_store ?? true;

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
        TTS. Enter the env-var name the provider reads (its <code className="font-mono text-[0.78rem] text-text">api_key_env</code>) and the key.
        It's encrypted with <code className="font-mono text-[0.78rem] text-text">DISCO_SECRET_KEY</code> and the plaintext never touches disk.
      </p>

      {!canStore && (
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
            <li
              key={n}
              className="flex items-center justify-between gap-inline rounded-control border border-hairline bg-surface-1 px-body py-inline"
            >
              <span className="flex items-center gap-hair font-mono text-[0.82rem] text-text">
                <KeyRound className="size-3.5 text-supported" aria-hidden /> {n}
              </span>
              <button
                type="button"
                onClick={() => clearSecret.mutate(n)}
                className="font-ui text-[0.8rem] text-text-muted hover:text-unsupported"
              >
                Clear
              </button>
            </li>
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
