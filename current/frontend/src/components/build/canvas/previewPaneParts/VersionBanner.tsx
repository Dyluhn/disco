import { MonitorPlay, Undo2 } from "lucide-react";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { cn } from "@/lib/cn";

export function VersionBanner({
  displayedVersionSeq,
  restoringSeq,
  onBackToCurrent,
  onRollback,
}: {
  displayedVersionSeq: number | null;
  restoringSeq: number | null;
  onBackToCurrent: () => void;
  onRollback: (seq: number) => Promise<void>;
}) {
  if (displayedVersionSeq === null) return null;
  return (
    <div className="flex shrink-0 items-center justify-between gap-inline border-b border-warn/30 bg-warn/10 px-body py-hair">
      <span className="font-ui text-[0.78rem] text-warn">
        Viewing immutable v{displayedVersionSeq} (read-only)
      </span>
      <div className="flex items-center gap-inline">
        <button
          type="button"
          onClick={onBackToCurrent}
          className="flex items-center gap-hair font-ui text-[0.74rem] text-text-muted hover:text-text"
        >
          <MonitorPlay className="size-3" aria-hidden /> Back to current
        </button>
        <ConfirmDialog
          title={`Roll back to v${displayedVersionSeq}?`}
          description="Restores the workspace to this exact version. The rollback is saved as a new version."
          confirmLabel="Roll back"
          confirmContext="workspace-rollback"
          onConfirm={() => void onRollback(displayedVersionSeq)}
          trigger={
            <button
              type="button"
              disabled={restoringSeq !== null}
              className="flex items-center gap-hair rounded-control border border-warn/40 px-inline py-hair font-ui text-[0.74rem] text-warn disabled:opacity-50"
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
