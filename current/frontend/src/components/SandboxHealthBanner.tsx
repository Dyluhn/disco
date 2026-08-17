import { Link } from "react-router-dom";
import { AlertTriangle } from "lucide-react";
import { useSandboxHealth } from "@/hooks/useModels";

/**
 * App-shell banner that surfaces an UNREACHABLE sandbox BEFORE the user starts a run
 * that's doomed. The 2026-06-23 outage was discovered only by every build/agent run
 * failing with a cryptic "ssh: Could not resolve hostname :" — this makes the broken
 * sandbox visible up-front, naming the backend + the real reason, with a one-click
 * affordance to Settings → Sandbox.
 *
 * Non-blocking + non-dismissable: it polls the SAME reachability probe the run path
 * hits (so the banner and a real run agree), and clears itself the moment the host
 * comes back. Hidden while the active sandbox is reachable (the normal case) and while
 * the first probe is in flight (no flash of a false alarm before we know).
 */
export function SandboxHealthBanner() {
  const { data } = useSandboxHealth();
  // Hidden until we have a definitive verdict, and whenever the sandbox is reachable.
  if (!data || data.reachable) return null;

  return (
    <div
      role="alert"
      data-sandbox-unreachable={data.backend}
      className="flex shrink-0 flex-wrap items-center gap-x-inline gap-y-hair border-b border-unsupported/40 bg-unsupported/10 px-body py-inline text-[0.8rem] text-unsupported"
    >
      <AlertTriangle className="size-3.5 shrink-0" aria-hidden />
      <span className="font-ui">
        <span className="font-medium">{data.backend} sandbox unreachable</span>
        {data.detail ? <span className="text-text-muted"> — {data.detail}</span> : null}
        <span className="text-text-muted"> Builds and agent runs will fail until it's fixed.</span>
      </span>
      <Link
        to="/settings"
        data-disco-control="shell.sandbox-unreachable-settings"
        className="ml-auto rounded-control border border-unsupported/50 px-inline py-px font-ui font-medium text-unsupported transition-colors hover:bg-unsupported/15"
      >
        Open Settings → Sandbox
      </Link>
    </div>
  );
}
