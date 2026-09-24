import { MonitorPlay, Undo2 } from "lucide-react";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { cn } from "@/lib/cn";

export function VersionBanner({
  selectedVersionSeq,
  versions,
  displayedVersionSeq,
  restoringSeq,
  onBackToCurrent,
  onRollback,
}: {
  selectedVersionSeq: number | null;
  versions: readonly { seq: number }[];
  displayedVersionSeq: number | null;
  restoringSeq: number | null;
  onBackToCurrent: () => void;
  onRollback: (seq: number) => Promise<void>;
}) {
  // Rollback authority is the selected server-listed snapshot. The frame's
  // displayed version remains separate: minting/bootstrapping may be offline
  // or may leave a different, previously loaded version on screen.
  const selected = versions.find((version) => version.seq === selectedVersionSeq);
  if (!selected) return null;
  const versionSeq = selected.seq;
  return (
    <div className="flex shrink-0 items-center justify-between gap-inline border-b border-warn/30 bg-warn/10 px-body py-hair">
      <span className="font-ui text-[0.78rem] text-warn">
        {displayedVersionSeq === versionSeq
          ? `Viewing immutable v${versionSeq} (read-only)`
          : `Selected immutable v${versionSeq}; preview has not loaded this version.`}
      </span>
      <div className="flex items-center gap-inline">
        <button
          type="button"
          onClick={onBackToCurrent}
          className="flex min-h-11 items-center gap-hair font-ui text-[0.74rem] text-text-muted hover:text-text lg:min-h-0"
        >
          <MonitorPlay className="size-3" aria-hidden /> Back to current
        </button>
        <ConfirmDialog
          title={`Roll back to v${versionSeq}?`}
          description="Restores the workspace to this exact version. The rollback is saved as a new version."
          confirmLabel="Roll back"
          confirmContext="workspace-rollback"
          onConfirm={() => void onRollback(versionSeq)}
          trigger={
            <button
              type="button"
              disabled={restoringSeq !== null}
              className="flex min-h-11 items-center gap-hair rounded-control border border-warn/40 px-inline py-hair font-ui text-[0.74rem] text-warn disabled:opacity-50 lg:min-h-0"
            >
              <Undo2 className={cn("size-3", restoringSeq !== null && "animate-spin")} aria-hidden />
              {restoringSeq !== null ? "Rolling back…" : "Roll back"}
            </button>
          }
        />
      </div>
    </div>
  );
}
