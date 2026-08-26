/**
 * The Build surface's control-pane header: the status bar, the driver-outage
 * banner (autonomous/PAUSED landing), the H1 task title, and the finished/
 * resumed "download source" affordance. Extracted verbatim from BuildSurface.tsx
 * (PKG-12-FE-BUILD).
 */

import { Download } from "lucide-react";
import { AgentStatusBar } from "@/components/build/AgentStatusBar";
import { DriverOutageBanner } from "@/components/build/DriverOutageBanner";
import type { isolationForBackend } from "@/lib/isolation";
import type { useDownloadProject } from "@/hooks/useProjects";
import type { BuildController } from "./types";
import type { BuildFraming } from "@/components/BuildSurface";
import type { CommittedFinish } from "@/lib/committedFinish";

export function BuildSurfaceHeader({
  b,
  isolation,
  taskLabel,
  download,
  onDownloadClick,
  framing,
  committedFinish,
}: {
  b: BuildController;
  isolation: ReturnType<typeof isolationForBackend>;
  taskLabel: string | null;
  download: ReturnType<typeof useDownloadProject>;
  onDownloadClick: () => void;
  framing: BuildFraming;
  committedFinish: CommittedFinish | null;
}) {
  return (
    <div className="flex flex-col gap-inline px-body pt-section">
      <AgentStatusBar
        status={b.status}
        isolation={isolation}
        sandboxState={b.sandboxState ?? undefined}
        connectionState={b.connectionState}
        autonomous={b.autonomous}
        /* Assist-tier badge deliberately hidden at launch (deprecated
           control) — b.assist (server-derived, from the reducer's
           extras.assist) still exists and is computed; AgentStatusBar just
           no longer accepts/renders it. */
        onKill={b.kill}
        onStop={b.cancel}
        onResume={b.canResume ? b.resume : undefined}
        events={b.events}
        modelId={b.modelId}
        seq={b.maxSeq}
        surface={framing}
      />
      {/* Honest provider-outage label for the autonomous flavor: the run
          CONCLUDED at PAUSED (no ask gate), so the WHY renders here next to
          the status bar + Resume. Non-interactive — Resume is the wired path. */}
      {b.driverOutage && b.status === "PAUSED" && <DriverOutageBanner outage={b.driverOutage} />}
      <div className="flex items-center justify-between gap-inline">
        <h1 className="font-display text-[1.3rem] font-medium leading-tight tracking-tight text-text">
          {taskLabel}
        </h1>
        {/* Export the saved project as a zip — only available for resumed
            projects and finished builds (a snapshot must exist on disk). */}
        {b.cid && (b.resumed || b.status === "FINISHED") && committedFinish && (
          <div className="flex shrink-0 flex-col items-end gap-hair">
            <button
              type="button"
              onClick={onDownloadClick}
              disabled={download.isPending}
              aria-label="Download source"
              data-disco-control="build.export-zip"
              title="Download the project source (.zip)"
              className="flex max-lg:min-h-11 items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:text-text disabled:opacity-40"
            >
              <Download className="size-3.5" aria-hidden />
              {download.isPending ? "Preparing…" : "Download source"}
            </button>
            {download.error && (
              <span role="alert" className="font-ui text-[0.7rem] text-unsupported">
                Download failed — try again
              </span>
            )}
          </div>
        )}
        {b.cid && b.status === "FINISHED" && !committedFinish && (
          <span className="font-ui text-[0.72rem] text-text-faint">Preparing download…</span>
        )}
      </div>
    </div>
  );
}
