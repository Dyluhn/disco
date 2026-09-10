import { useState } from "react";

/** A workspace image with an honest onError→placeholder fallback (the screenshot
 * may have been pruned). Key by `src` in the parent so the failed state resets
 * when the selection changes. */
export function WorkspaceImage({ src, className }: { src: string; className: string }) {
  const [failed, setFailed] = useState(false);
  if (failed)
    return (
      <span
        data-screenshot-state="missing"
        className="font-ui text-[0.74rem] text-text-faint italic"
      >
        screenshot no longer available
      </span>
    );
  return (
    <img
      src={src}
      alt="browser screenshot"
      data-screenshot-state="present"
      onError={() => setFailed(true)}
      className={className}
    />
  );
}
