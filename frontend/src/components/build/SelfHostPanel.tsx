/**
 * Self-host handoff — turns a release verdict (`ReleaseResponse`, WO-7/WO-8) into
 * an honest "here's how you run this yourself" panel. It is PURELY capability-
 * driven: it renders from the release DATA alone and takes no knowledge of which
 * mode or workspace produced the project, so it is byte-identical wherever it is
 * mounted. The parent owns the one real action (download the source zip) and wires
 * it in via `onDownload`.
 *
 * No false affordance (project rule): the run command + env list appear ONLY when
 * the release is genuinely self-hostable (`self_host`); otherwise the panel is
 * honest about WHY (blockers when review is needed, the plain reason when it is not
 * a web app) and never shows a self-host-ready control that does nothing. The one
 * button — "Download source" — is always a real, wired action (the source zip is
 * always retrievable), so no state renders a dead button.
 *
 * Secret hygiene (inherited from the wire type): a `required_env` entry carries a
 * NAME + metadata only — there is deliberately no value field. This panel renders
 * the names (and whether each is a secret to be injected out-of-band) and never
 * fabricates or displays a value.
 */

import { AlertTriangle, Download, Info, KeyRound, Server, Terminal } from "lucide-react";
import type { ReleaseResponse } from "@/types/release";

/** "Ready to self-host" is reserved for a VERIFIED release — verification has
 * actually run the project. A `candidate` is statically plausible and UNVERIFIED,
 * so its bundle is only "available", never "ready" (locked semantic §2.1/§2.2). */
function StatusPill({ release }: { release: ReleaseResponse }) {
  const verified = release.assessment === "verified";
  const status = verified
    ? "ready"
    : release.self_host
      ? "candidate"
      : release.blockers.length > 0
        ? "needs_review"
        : "not_web";
  const label = verified
    ? "Ready to self-host"
    : release.self_host
      ? "Bundle available"
      : release.blockers.length > 0
        ? "Needs review"
        : "Not a web app";
  return (
    <span
      data-self-host-status={status}
      className={
        verified || release.self_host
          ? "shrink-0 rounded-full border border-accent/40 px-inline py-px font-ui text-[0.66rem] uppercase tracking-wide text-accent"
          : "shrink-0 rounded-full border border-hairline px-inline py-px font-ui text-[0.66rem] uppercase tracking-wide text-text-faint"
      }
    >
      {label}
    </span>
  );
}

export function SelfHostPanel({
  release,
  onDownload,
}: {
  release: ReleaseResponse;
  onDownload: () => void;
}) {
  // The bundle is downloadable/runnable when the release is self-hostable; the
  // honest "Ready" claim is tied to VERIFICATION (assessment === "verified"), NOT
  // to `self_host`. A `candidate` self-hosts a bundle but stays "Not runtime-verified".
  const selfHostable = release.self_host;
  const verified = release.assessment === "verified";
  const reason = release.reasons[0] ?? null;

  return (
    <section
      data-disco-control="build.self-host"
      data-self-host-assessment={release.assessment}
      aria-label="Self-host this project"
      className="flex flex-col gap-inline rounded-control border border-hairline bg-accent/5 px-body py-inline"
    >
      <div className="flex items-center gap-hair">
        <Server className="size-4 shrink-0 text-accent" aria-hidden />
        <span className="flex-1 font-ui text-[0.86rem] font-medium text-text">Self-host</span>
        <StatusPill release={release} />
      </div>

      {selfHostable ? (
        <div className="flex flex-col gap-inline">
          {reason && <p className="font-ui text-[0.78rem] text-text-muted">{reason}</p>}
          {!verified && (
            // A candidate is statically plausible but UNVERIFIED — it never claims
            // to be "Ready" (§2.1). This qualifier is the honest counterpart.
            <p
              data-self-host-note="unverified"
              className="font-ui text-[0.74rem] text-text-faint"
            >
              Not runtime-verified — the self-host bundle is available but has not been run.
            </p>
          )}
          <div className="flex flex-col gap-hair">
            <span className="font-ui text-[0.72rem] font-medium uppercase tracking-wide text-text-faint">
              Run it
            </span>
            <div className="flex items-center gap-hair overflow-x-auto rounded-control border border-hairline bg-bg px-inline py-hair">
              <Terminal className="size-3.5 shrink-0 text-text-faint" aria-hidden />
              <code
                data-disco-control="build.self-host-command"
                className="whitespace-pre font-mono text-[0.78rem] text-text"
              >
                {release.command}
              </code>
            </div>
          </div>
          {release.required_env.length > 0 && (
            <div className="flex flex-col gap-hair">
              <span className="font-ui text-[0.72rem] font-medium uppercase tracking-wide text-text-faint">
                Environment variables to set
              </span>
              <ul aria-label="Required environment variables" className="flex flex-col gap-hair">
                {release.required_env.map((env) => (
                  <li
                    key={env.name}
                    data-env-name={env.name}
                    className="flex flex-wrap items-center gap-hair font-ui text-[0.76rem] text-text-muted"
                  >
                    {/* NAME only — the wire type carries no value, and the panel
                        never fabricates one (secret hygiene). */}
                    <code className="font-mono text-[0.76rem] text-text">{env.name}</code>
                    <span className="rounded-full border border-hairline px-inline py-px text-[0.66rem] text-text-faint">
                      {env.scope}
                    </span>
                    <span className="rounded-full border border-hairline px-inline py-px text-[0.66rem] text-text-faint">
                      {env.required ? "required" : "optional"}
                    </span>
                    {env.secret && (
                      <span className="flex items-center gap-px rounded-full border border-hairline px-inline py-px text-[0.66rem] text-text-faint">
                        <KeyRound className="size-3" aria-hidden />
                        secret
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      ) : release.blockers.length > 0 ? (
        // Review needed: name each blocker as flat data. Deliberately NO run
        // command and NO self-host-ready control — presenting one would be a false
        // affordance (it isn't runnable yet).
        <div className="flex flex-col gap-hair">
          <span className="flex items-center gap-hair font-ui text-[0.76rem] font-medium text-warn">
            <AlertTriangle className="size-3.5 shrink-0" aria-hidden />
            Resolve these before it can be self-hosted:
          </span>
          <ul className="flex flex-col gap-hair">
            {release.blockers.map((blocker, i) => (
              <li
                key={`${blocker.code}-${i}`}
                data-blocker-code={blocker.code}
                className="flex flex-col font-ui text-[0.76rem] text-text-muted"
              >
                <span>{blocker.message}</span>
                {/* A structured path (e.g. an overlay-collision path — needed by C6)
                    is surfaced verbatim when the finding names one. */}
                {blocker.path && (
                  <code
                    data-blocker-path={blocker.path}
                    className="font-mono text-[0.72rem] text-text-faint"
                  >
                    {blocker.path}
                  </code>
                )}
              </li>
            ))}
          </ul>
        </div>
      ) : (
        // Not a web app: the honest reason, and nothing that pretends otherwise.
        <p className="flex items-start gap-hair font-ui text-[0.78rem] text-text-muted">
          <Info className="mt-px size-3.5 shrink-0 text-text-faint" aria-hidden />
          {reason ?? "This project has no web server to self-host."}
        </p>
      )}

      {/* The one real action, valid in every state: the source is always
          downloadable. Wired by the parent; never a dead button. */}
      <div className="flex justify-end">
        <button
          type="button"
          onClick={onDownload}
          aria-label="Download source"
          title="Download the project source (.zip)"
          data-disco-control="build.download-source"
          className="flex items-center gap-hair rounded-control border border-accent/50 px-inline py-hair font-ui text-[0.8rem] font-medium text-accent transition-colors hover:bg-accent/10"
        >
          <Download className="size-3.5" aria-hidden />
          Download source
        </button>
      </div>
    </section>
  );
}
