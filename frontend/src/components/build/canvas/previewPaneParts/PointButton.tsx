import { MousePointer2 } from "lucide-react";
import { cn } from "@/lib/cn";

export function PointButton({
  armed,
  onArm,
  onDisarm,
}: {
  armed: boolean;
  onArm: () => void;
  onDisarm: () => void;
}) {
  return (
    <button
      type="button"
      onClick={armed ? onDisarm : onArm}
      aria-pressed={armed}
      aria-label={armed ? "Cancel element mention" : "Point at element"}
      data-disco-control="build.element-mention"
      className={cn(
        "flex min-h-11 items-center gap-hair font-ui text-[0.74rem] transition-colors lg:min-h-0",
        armed ? "text-accent" : "text-text-muted hover:text-text",
      )}
    >
      <MousePointer2 className="size-3" aria-hidden />
      {armed ? "Cancel" : "Point"}
    </button>
  );
}
