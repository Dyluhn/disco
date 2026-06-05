/**
 * The confirmation UI (the ConfirmRisky gate, made visible + controllable). When the
 * loop pauses at WAITING_FOR_CONFIRMATION, the human decides WITH the risk rationale in
 * view — Approve runs exactly that action; Reject records a denial and the loop resumes.
 */

import { ShieldAlert } from "lucide-react";
import { cn } from "@/lib/cn";
import type { ActionEvent, SecurityRisk } from "@/types/agent";

const RISK_STYLE: Record<SecurityRisk, { box: string; text: string; label: string }> = {
  HIGH: { box: "border-unsupported", text: "text-unsupported", label: "High risk" },
  MEDIUM: { box: "border-weak", text: "text-weak", label: "Medium risk" },
  LOW: { box: "border-hairline-strong", text: "text-text-muted", label: "Low risk" },
  UNKNOWN: { box: "border-weak", text: "text-weak", label: "Unknown risk" },
};

function summarize(action: ActionEvent): string {
  const tc = action.tool_call;
  if (!tc) return "(no action)";
  if (tc.tool_name === "shell") return String(tc.arguments.command ?? "");
  if (tc.tool_name === "browser")
    return `${tc.arguments.action ?? ""} ${tc.arguments.url ?? ""}`.trim();
  if (tc.tool_name.startsWith("file_")) return String(tc.arguments.path ?? "");
  return JSON.stringify(tc.arguments);
}

export function ConfirmationPanel({
  action,
  onApprove,
  onReject,
}: {
  action: ActionEvent;
  onApprove: () => void;
  onReject: () => void;
}) {
  const assessment = action.meta?.risk_assessment;
  const risk: SecurityRisk = assessment?.risk ?? "UNKNOWN";
  const style = RISK_STYLE[risk];

  return (
    <section
      role="alertdialog"
      aria-label="Action needs your approval"
      className={cn("rounded-card border-2 bg-surface-1 px-body py-body", style.box)}
    >
      <header className="flex items-center gap-inline">
        <ShieldAlert className={cn("size-4 shrink-0", style.text)} aria-hidden />
        <h2 className="font-ui text-[0.9rem] font-medium text-text">This action needs your approval</h2>
        <span
          className={cn(
            "ml-auto rounded-full border px-inline py-px font-ui text-[0.7rem] uppercase tracking-wide",
            style.box,
            style.text,
          )}
        >
          {style.label}
        </span>
      </header>

      <code className="mt-inline block w-full overflow-x-auto rounded-control bg-surface-2 px-inline py-hair font-mono text-[0.8rem] text-text">
        <span className="text-text-faint">{action.tool_call?.tool_name}&nbsp;</span>
        {summarize(action)}
      </code>

      {assessment && (
        <p className="mt-inline font-ui text-[0.8rem] leading-snug text-text-muted">
          <span className="text-text-faint">Why: </span>
          {assessment.rationale}
          <span className="text-text-faint"> · {assessment.analyzer}</span>
        </p>
      )}

      <div className="mt-body flex items-center justify-end gap-inline">
        <button
          type="button"
          onClick={onReject}
          className="rounded-control border border-hairline px-body py-hair font-ui text-[0.82rem] text-text-muted transition-colors hover:text-text"
        >
          Reject
        </button>
        <button
          type="button"
          onClick={onApprove}
          className="rounded-control bg-accent px-body py-hair font-ui text-[0.82rem] font-medium text-bg transition-opacity hover:opacity-90"
        >
          Approve &amp; run
        </button>
      </div>
    </section>
  );
}
