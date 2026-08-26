import { CheckCircle2 } from "lucide-react";
import type { WorkflowValidationFinding } from "@/types/workflow";

export function FindingRows({ findings }: { findings: WorkflowValidationFinding[] }) {
  if (findings.length === 0) {
    return (
      <div className="flex items-center gap-hair font-ui text-[0.8rem] text-text-muted">
        <CheckCircle2 className="size-4 text-supported" aria-hidden />
        No issues found.
      </div>
    );
  }
  return (
    <ul className="flex flex-col gap-hair">
      {findings.map((finding, index) => (
        <li
          key={`${finding.code}-${finding.path}-${index}`}
          className="rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.8rem]"
          data-severity={finding.severity}
        >
          <span
            className={
              finding.severity === "error"
                ? "font-semibold text-unsupported"
                : "font-semibold text-warn"
            }
          >
            {finding.severity === "error" ? "Needs attention" : "Review"}
          </span>
          <div className="text-text">{finding.message}</div>
        </li>
      ))}
    </ul>
  );
}
