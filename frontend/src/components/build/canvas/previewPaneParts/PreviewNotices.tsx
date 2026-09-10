import { cn } from "@/lib/cn";
import { shouldShowUpdateErrorBanner } from "./helpers";

/** The two host-owned strips above the stage: the workspace-rollback result and
 * the platform's "still retrying an update" warning. Both are pure copy driven
 * by plain values — the rollback notice is passed as its own shape rather than
 * the rollback hook's handle, so this part stays in the same bounded context as
 * the pane. */
export function PreviewNotices({
  restoreNotice,
  selectedVersionSeq,
  updateError,
}: {
  restoreNotice: { tone: "success" | "error"; text: string } | null;
  selectedVersionSeq: number | null;
  updateError: string | null | undefined;
}) {
  return (
    <>
      {restoreNotice && (
        <div
          role={restoreNotice.tone === "error" ? "alert" : "status"}
          className={cn(
            "shrink-0 border-b px-body py-hair font-ui text-[0.72rem]",
            restoreNotice.tone === "error"
              ? "border-unsupported/30 bg-unsupported/10 text-unsupported"
              : "border-supported/30 bg-supported/10 text-supported",
          )}
        >
          {restoreNotice.text}
        </div>
      )}
      {shouldShowUpdateErrorBanner(selectedVersionSeq, updateError) && (
        <div
          role="alert"
          className="shrink-0 border-b border-warn/30 bg-warn/10 px-body py-hair font-ui text-[0.72rem] text-warn"
        >
          Preview is showing the last healthy frame while the platform retries: {updateError}
        </div>
      )}
    </>
  );
}
