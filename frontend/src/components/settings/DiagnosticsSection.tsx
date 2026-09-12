/**
 * Settings → Diagnostics: "is everything OK, and which build is this?"
 *
 * One view of the facts a bug report needs: build identity, both servers' health,
 * the active sandbox probe, and the doctor's in-process checks (config, data disk,
 * database, secret key), plus one button that copies the whole payload as JSON.
 * Everything shown is fetched, never inferred; a fetch failure is shown as such.
 * The full doctor (driver-model check, support bundle) stays a command:
 * `python -m disco.agent_server.doctor --bundle …` — the section says so.
 */
import { AlertTriangle, Check, Copy, Loader2, XCircle } from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/cn";
import { useAppServerHealth, useDiagnostics } from "@/hooks/useDiagnostics";
import type { AppServerHealth, DiagnosticCheck, Diagnostics } from "@/types/diagnostics";

const DOCTOR_COMMAND =
  "podman compose exec agent-server python -m disco.agent_server.doctor --bundle /tmp/disco-doctor.json";

function tone(status: string): string {
  if (status === "PASS" || status === "ok") return "text-supported";
  if (status === "WARN" || status === "SKIP") return "text-text-muted";
  return "text-unsupported";
}

function StatusIcon({ status }: { status: string }) {
  if (status === "PASS" || status === "ok") return <Check className="size-3.5" aria-hidden />;
  if (status === "WARN" || status === "SKIP") return <AlertTriangle className="size-3.5" aria-hidden />;
  return <XCircle className="size-3.5" aria-hidden />;
}

function Row({ name, status, detail }: DiagnosticCheck) {
  return (
    <li className="grid grid-cols-[6rem_1fr] gap-x-body gap-y-hair py-inline sm:grid-cols-[8rem_1fr]">
      <span className={cn("inline-flex items-center gap-hair font-ui text-[0.78rem] font-semibold uppercase tracking-wide", tone(status))}>
        <StatusIcon status={status} />
        <span data-check-status={status}>{status}</span>
      </span>
      <span className="font-ui text-[0.86rem] text-text">
        <span className="text-text-muted">{name}</span> — {detail}
      </span>
    </li>
  );
}

type AppHealthState = { data?: AppServerHealth; isError: boolean };

/** One row per fact, in the order a reader checks them: this server, the other server,
 * the sandbox, then the doctor's local checks. Pure, so the component stays small. */
function buildRows(diagnostics: Diagnostics | undefined, app: AppHealthState): DiagnosticCheck[] {
  const rows: DiagnosticCheck[] = [];
  if (diagnostics) {
    const agent = diagnostics.agent_server;
    const ok = agent.status === "ok";
    rows.push({
      name: "agent-server",
      status: ok ? "PASS" : "FAIL",
      detail: ok ? `ok, version ${agent.version}` : `degraded: ${JSON.stringify(agent.checks)}`,
    });
  }
  if (app.data) {
    const ok = app.data.status === "ok";
    rows.push({ name: "app-server", status: ok ? "PASS" : "FAIL", detail: `${app.data.status}, version ${app.data.version}` });
  } else if (app.isError) {
    rows.push({ name: "app-server", status: "FAIL", detail: "unreachable from this page" });
  }
  if (diagnostics) {
    const sandbox = diagnostics.sandbox;
    rows.push({
      name: "sandbox",
      status: sandbox.reachable ? "PASS" : "FAIL",
      detail: sandbox.reachable ? `${sandbox.backend} reachable` : `${sandbox.backend}: ${sandbox.detail}`,
    });
    rows.push(...diagnostics.checks);
  }
  return rows;
}

const COPY_LABEL = { idle: "Copy diagnostics", copied: "Copied", failed: "Copy failed" } as const;

function buildLine(diagnostics: Diagnostics | undefined, isError: boolean): string {
  if (diagnostics) return `Build ${diagnostics.version} (${diagnostics.build.source})`;
  return isError ? "The agent-server did not answer the diagnostics request." : "Loading…";
}

export function DiagnosticsSection() {
  const diagnostics = useDiagnostics();
  const appHealth = useAppServerHealth();
  const [copied, setCopied] = useState<keyof typeof COPY_LABEL>("idle");
  const rows = buildRows(diagnostics.data, { data: appHealth.data, isError: appHealth.isError });

  async function copy() {
    const payload = { diagnostics: diagnostics.data ?? null, app_server: appHealth.data ?? null };
    try {
      await navigator.clipboard.writeText(JSON.stringify(payload, null, 2));
      setCopied("copied");
    } catch {
      setCopied("failed");
    }
    window.setTimeout(() => setCopied("idle"), 2500);
  }

  return (
    <section aria-labelledby="diagnostics-heading" className="flex flex-col gap-body">
      <div className="flex flex-wrap items-start justify-between gap-body">
        <div>
          <h3 id="diagnostics-heading" className="font-display text-[1.05rem] text-text">
            Build and health
          </h3>
          <p className="mt-hair font-ui text-[0.86rem] text-text-muted">{buildLine(diagnostics.data, diagnostics.isError)}</p>
        </div>
        <button
          type="button"
          onClick={copy}
          disabled={!diagnostics.data}
          className="inline-flex items-center gap-hair rounded-control border border-hairline bg-surface-1 px-body py-inline font-ui text-[0.82rem] text-text hover:bg-surface-2 disabled:opacity-50"
        >
          {copied === "copied" ? <Check className="size-3.5" aria-hidden /> : <Copy className="size-3.5" aria-hidden />}
          {COPY_LABEL[copied]}
        </button>
      </div>

      {diagnostics.isPending ? (
        <p className="inline-flex items-center gap-hair font-ui text-[0.86rem] text-text-muted">
          <Loader2 className="size-3.5 animate-spin" aria-hidden /> Checking…
        </p>
      ) : diagnostics.isError ? (
        <p className="font-ui text-[0.86rem] text-unsupported" role="alert">
          Diagnostics unavailable: {diagnostics.error instanceof Error ? diagnostics.error.message : String(diagnostics.error)}
        </p>
      ) : (
        <ul className="divide-y divide-hairline" aria-label="Diagnostic checks">
          {rows.map((row) => (
            <Row key={row.name} {...row} />
          ))}
        </ul>
      )}

      <p className="font-ui text-[0.8rem] text-text-faint">
        For the driver-model check and a redacted support bundle, run in a terminal:{" "}
        <code className="rounded-[0.25rem] border border-hairline bg-surface-2 px-1 font-mono text-[0.78em]">{DOCTOR_COMMAND}</code>
        . The bundle contains no conversation content.
      </p>
    </section>
  );
}
