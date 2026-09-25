import { useState } from "react";
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
      {shots.map((path, index) => (
        <ScreenshotThumbnail
          key={`${cid}-${path}-${index}`}
          src={workspaceFileUrl(cid, path)}
          number={index + 1}
          selected={path === hero}
          onPick={() => onPick(path)}
        />
      ))}
    </div>
  );
}


function ScreenshotThumbnail({ src, number, selected, onPick }: {
  src: string;
  number: number;
  selected: boolean;
  onPick: () => void;
}) {
  const [failed, setFailed] = useState(false);
  return (
    <button
      type="button"
      onClick={onPick}
      aria-label={`View screenshot ${number}${failed ? " (thumbnail unavailable)" : ""}`}
      aria-pressed={selected}
      className={cn(
        "h-12 w-20 shrink-0 overflow-hidden rounded border transition-colors",
        selected ? "border-accent" : "border-hairline hover:border-hairline-strong",
      )}
    >
      {failed ? (
        <span className="flex h-full items-center justify-center px-hair text-center font-ui text-[0.65rem] text-text-muted">
          Thumbnail unavailable
        </span>
      ) : (
        <img src={src} alt="" onError={() => setFailed(true)} className="h-full w-full object-cover" />
      )}
    </button>
  );
}
