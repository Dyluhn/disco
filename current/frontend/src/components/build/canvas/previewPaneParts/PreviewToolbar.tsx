import { ExternalLink, Pencil, RotateCw } from "lucide-react";
import type { WorkspaceVersion } from "@/api/agent";
import { cn } from "@/lib/cn";
import { PointButton } from "./PointButton";
import { VersionPicker } from "./VersionPicker";

export function PreviewToolbar({
  versions,
  selectedVersionSeq,
  onSelectVersion,
  onRefresh,
  restarting,
  cid,
  showPointButton,
  mentionArmed,
  onArmMention,
  onDisarmMention,
  canEdit,
  editMode,
  onToggleEdit,
  onOpenPreview,
}: {
  versions: WorkspaceVersion[];
  selectedVersionSeq: number | null;
  onSelectVersion: (seq: number | null) => void;
  onRefresh: () => void;
  restarting: boolean;
  cid: string | null;
  showPointButton: boolean;
  mentionArmed: boolean;
  onArmMention: () => void;
  onDisarmMention: () => void;
  canEdit: boolean;
  editMode: boolean;
  onToggleEdit: () => void;
  onOpenPreview: () => void;
}) {
  return (
    <div className="flex shrink-0 items-center justify-between gap-inline border-b border-hairline px-body py-hair">
      <span className="truncate font-mono text-[0.74rem] text-text-faint">Preview</span>
      <div className="flex items-center gap-inline">
        <VersionPicker versions={versions} selectedSeq={selectedVersionSeq} onSelect={onSelectVersion} />
        <button
          type="button"
          onClick={onRefresh}
          disabled={restarting || !cid}
          aria-label="Refresh or restart Preview"
          data-disco-control="build.preview-refresh"
          className="flex items-center gap-hair font-ui text-[0.74rem] text-text-muted transition-colors hover:text-text disabled:opacity-50"
        >
          <RotateCw className={cn("size-3", restarting && "animate-spin")} aria-hidden />
          {restarting ? "Restarting…" : "Refresh"}
        </button>
        {showPointButton && (
          <PointButton armed={mentionArmed} onArm={onArmMention} onDisarm={onDisarmMention} />
        )}
        {canEdit && (
          <button
            type="button"
            onClick={onToggleEdit}
            aria-pressed={editMode}
            aria-label="Toggle click-to-edit mode on Preview"
            data-disco-control="build.edit-toggle"
            className={cn(
              "flex items-center gap-hair font-ui text-[0.74rem] transition-colors",
              editMode ? "text-accent" : "text-text-muted hover:text-text",
            )}
          >
            <Pencil className="size-3" aria-hidden /> Edit
          </button>
        )}
        {cid && (
          <button
            type="button"
            onClick={onOpenPreview}
            aria-label="Open Preview in new tab"
            data-disco-control="build.preview-open"
            className="flex items-center gap-hair font-ui text-[0.74rem] text-text-muted transition-colors hover:text-text"
          >
            <ExternalLink className="size-3" aria-hidden /> Open
          </button>
        )}
      </div>
    </div>
  );
}
