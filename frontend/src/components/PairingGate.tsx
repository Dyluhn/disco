/**
 * First-run auth gate. On mount it bootstraps a session against the live
 * backend(s). Three outcomes:
 *   - fixture/demo mode or an already-valid session  → render the app.
 *   - the deployment needs pairing (containerized / remote self-host, where the
 *     browser is never on the server's loopback so the token can't be
 *     auto-fetched) → prompt for the one-time token the server printed to its
 *     logs, pair, then render the app.
 *   - a real connection error → show it with a retry.
 *
 * Without this gate a remote/self-host browser can never obtain a session, so
 * every authed call fails and the whole UI shows "Couldn't load …" (the
 * 2026-07-09 fresh-install bug).
 */

import { useCallback, useEffect, useState } from "react";
import { KeyRound, Loader2 } from "lucide-react";
import {
  PairingRequiredError,
  bootstrapSessions,
  isDemoMode,
  pairWithToken,
} from "@/api/client";

type Phase = "checking" | "ready" | "pairing" | "error";

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-dvh items-center justify-center bg-bg px-body">
      <div className="flex w-[min(30rem,92vw)] flex-col gap-body rounded-card border border-hairline bg-surface-1 p-section">
        {children}
      </div>
    </div>
  );
}

export function PairingGate({ children }: { children: React.ReactNode }) {
  // Fixture/demo mode (no backend configured — tests, offline) has no auth to do:
  // start READY so children render on the first synchronous tick, no flash, no
  // network. Only a live deployment enters the async "checking" bootstrap.
  const demo = isDemoMode();
  const [phase, setPhase] = useState<Phase>(demo ? "ready" : "checking");
  const [token, setToken] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const boot = useCallback(async () => {
    setPhase("checking");
    setError(null);
    try {
      await bootstrapSessions();
      setPhase("ready");
    } catch (e) {
      if (e instanceof PairingRequiredError) {
        setPhase("pairing");
      } else {
        setError(e instanceof Error ? e.message : String(e));
        setPhase("error");
      }
    }
  }, []);

  useEffect(() => {
    if (!demo) void boot();
  }, [demo, boot]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const t = token.trim();
    if (!t || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await pairWithToken(t);
      await boot();
    } catch (err) {
      // A wrong/expired token comes back as PairingRequiredError; anything else
      // is a real failure. Either way, keep the user on the form with a reason.
      setError(
        err instanceof PairingRequiredError
          ? "That token was not accepted. Copy the current token from your server logs and try again."
          : err instanceof Error
            ? err.message
            : String(err),
      );
    } finally {
      setSubmitting(false);
    }
  };

  if (phase === "ready") return <>{children}</>;

  if (phase === "checking") {
    return (
      <Centered>
        <div className="flex items-center gap-inline text-text-muted">
          <Loader2 className="size-4 animate-spin" aria-hidden />
          <span className="font-ui text-[0.9rem]">Connecting…</span>
        </div>
      </Centered>
    );
  }

  if (phase === "error") {
    return (
      <Centered>
        <h1 className="font-ui text-[1.05rem] font-semibold text-text">
          Can't reach the server
        </h1>
        <p className="font-ui text-[0.86rem] leading-snug text-text-muted">
          The Disco backend didn't respond. Check that the app-server and
          agent-server containers are running, then retry.
        </p>
        {error && (
          <p className="rounded-control border border-hairline bg-surface-2 px-inline py-hair font-mono text-[0.76rem] text-unsupported">
            {error}
          </p>
        )}
        <button
          type="button"
          onClick={() => void boot()}
          className="self-start rounded-control bg-accent px-body py-hair font-ui text-[0.85rem] font-medium text-bg transition-opacity hover:opacity-90"
        >
          Retry
        </button>
      </Centered>
    );
  }

  // phase === "pairing"
  return (
    <Centered>
      <div className="flex items-center gap-inline">
        <KeyRound className="size-5 text-accent" aria-hidden />
        <h1 className="font-ui text-[1.05rem] font-semibold text-text">
          Pair this browser
        </h1>
      </div>
      <p className="font-ui text-[0.86rem] leading-snug text-text-muted">
        This is a first-run setup. Paste the one-time pairing token the server
        printed on startup — it authorizes this browser as the admin.
      </p>
      <p className="font-ui text-[0.78rem] leading-snug text-text-faint">
        Find it in your server logs:
        <code className="ml-hair rounded-control bg-surface-2 px-hair py-px font-mono text-[0.74rem] text-text-muted">
          docker compose logs app-server | grep pairing
        </code>
      </p>
      <form onSubmit={submit} className="flex flex-col gap-inline">
        <input
          autoFocus
          value={token}
          onChange={(e) => setToken(e.target.value)}
          spellCheck={false}
          placeholder="pairing token"
          aria-label="Pairing token"
          data-disco-control="auth.pairing-token"
          className="rounded-control border border-hairline bg-surface-2 px-inline py-inline font-mono text-[0.82rem] text-text outline-none transition-colors focus:border-accent"
        />
        {error && (
          <p role="alert" className="font-ui text-[0.78rem] text-unsupported">
            {error}
          </p>
        )}
        <button
          type="submit"
          disabled={!token.trim() || submitting}
          className="flex items-center justify-center gap-inline rounded-control bg-accent px-body py-hair font-ui text-[0.85rem] font-medium text-bg transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          {submitting && <Loader2 className="size-4 animate-spin" aria-hidden />}
          {submitting ? "Pairing…" : "Pair browser"}
        </button>
      </form>
    </Centered>
  );
}
