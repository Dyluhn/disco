import { cn } from "@/lib/cn";
import { workspaceFileUrl } from "@/api/canvas";

/** The scrubber row of past screenshots below the hero image — only shown once
 * there's more than one to choose between. */
export function ScreenshotStrip({
  shots,
  hero,
  cid,
  onPick,
}: {
  shots: string[];
  hero: string | null;
  cid: string;
  onPick: (path: string) => void;
}) {
  if (shots.length <= 1) return null;
  return (
    <div className="flex shrink-0 items-center gap-hair overflow-x-auto border-t border-hairline px-body py-hair">
      {shots.map((p, i) => {
        const src = workspaceFileUrl(cid, p);
        return (
          <button
            key={`${p}-${i}`}
            type="button"
            onClick={() => onPick(p)}
            className={cn(
              "shrink-0 overflow-hidden rounded border transition-colors",
              p === hero ? "border-accent" : "border-hairline hover:border-hairline-strong",
            )}
          >
            <img src={src} alt="" className="h-12 w-auto object-cover" />
          </button>
        );
      })}
    </div>
  );
}
