import { X } from "lucide-react";
import { elementMentionLabel } from "@/lib/elementMention";
import type { ElementMentionPayload } from "@/lib/elementMention";

export function ElementMentionChip({
  mention,
  onRemove,
}: {
  mention: ElementMentionPayload | null;
  onRemove: () => void;
}) {
  if (!mention) return null;
  return (
    <div
      className="flex min-w-0 items-center gap-hair rounded-control border border-accent/40 bg-accent/5 px-inline py-hair font-ui text-[0.76rem] text-text"
      data-testid="element-mention-chip"
    >
      <span className="min-w-0 flex-1 truncate">{elementMentionLabel(mention)}</span>
      <button
        type="button"
        onClick={onRemove}
        aria-label="Remove mentioned element"
        className="inline-flex min-h-11 min-w-11 shrink-0 items-center justify-center rounded p-px text-text-faint transition-colors hover:text-text lg:min-h-0 lg:min-w-0"
      >
        <X className="size-3" aria-hidden />
      </button>
    </div>
  );
}
