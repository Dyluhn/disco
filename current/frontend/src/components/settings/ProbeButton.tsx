/**
 * The shared live "Test" affordance for the Settings provider sections (T4.x).
 *
 * Replaces the old honest-disabled `data-disco-flag` placeholders: clicking runs
 * a REAL backend probe and renders the truth — idle → testing → ok/failed — with
 * the provider's own message. A green chip only appears when the probe returned
 * `ok: true` (the provider really answered); every other outcome is shown as the
 * honest failure it is. No fake green.
 *
 * Test/automation handles: the button carries `data-disco-control={control}`; the
 * status chip carries `data-probe-status` (idle|testing|<status>|failed) and
 * `data-probe-ok` so a Playwright/vitest run can assert the live result.
 */
import { AlertTriangle, Check, FlaskConical, Loader2 } from "lucide-react";
import { useState } from "react";
import { cn } from "@/lib/cn";
import type { ProbeResult } from "@/types/probe";

type Phase =
  | { phase: "idle" }
  | { phase: "testing" }
  | { phase: "done"; result: ProbeResult }
  | { phase: "failed"; message: string };

export function ProbeButton({
  control,
  run,
  disabled = false,
  idleLabel = "Test",
  disabledHint,
  className,
}: {
  control: string;
  run: () => Promise<ProbeResult>;
  disabled?: boolean;
  idleLabel?: string;
  /** Shown next to the button when `disabled` (e.g. "connect a backend to test"). */
  disabledHint?: string;
  className?: string;
}) {
  const [state, setState] = useState<Phase>({ phase: "idle" });

  const onClick = async () => {
    setState({ phase: "testing" });
    try {
      const result = await run();
      setState({ phase: "done", result });
    } catch (e) {
      // A thrown error = the probe endpoint itself was unreachable (network/500).
      setState({
        phase: "failed",
        message: e instanceof Error ? e.message : "The probe request failed.",
      });
    }
  };

  // The chip's machine-readable status + ok flag for development/tests/automation.
  const statusAttr =
    state.phase === "done" ? state.result.status : state.phase === "failed" ? "failed" : state.phase;
  const okAttr = state.phase === "done" ? state.result.ok : false;

  let chip: React.ReactNode = null;
  if (state.phase === "testing") {
    chip = (
      <span className="flex items-center gap-hair font-ui text-[0.76rem] text-text-muted">
        <Loader2 className="size-3 animate-spin" aria-hidden /> Testing…
      </span>
    );
  } else if (state.phase === "failed") {
    chip = (
      <span className="flex items-center gap-hair font-ui text-[0.76rem] text-unsupported">
        <AlertTriangle className="size-3 shrink-0" aria-hidden /> {state.message}
      </span>
    );
  } else if (state.phase === "done") {
    const { ok, status, detail } = state.result;
    chip = (
      <span
        className={cn(
          "flex items-center gap-hair font-ui text-[0.76rem]",
          ok ? "text-supported" : "text-unsupported",
        )}
      >
        {ok ? (
          <Check className="size-3 shrink-0" aria-hidden />
        ) : (
          <AlertTriangle className="size-3 shrink-0" aria-hidden />
        )}
        <span className="font-medium capitalize">{status.replace(/-/g, " ")}</span>
        {detail ? <span className="text-text-muted">— {detail}</span> : null}
      </span>
    );
  }

  return (
    <div className={cn("flex flex-wrap items-center gap-inline", className)}>
      <button
        type="button"
        data-disco-control={control}
        disabled={disabled || state.phase === "testing"}
        onClick={onClick}
        className={cn(
          "flex shrink-0 items-center gap-hair rounded-control border px-inline py-hair font-ui text-[0.8rem] transition-colors",
          disabled
            ? "border-hairline text-text-faint"
            : "border-hairline text-text-muted hover:border-accent hover:text-text",
        )}
      >
        <FlaskConical className="size-3.5" aria-hidden />
        {idleLabel}
      </button>
      <span aria-live="polite" data-probe-status={statusAttr} data-probe-ok={okAttr}>
        {chip}
        {disabled && disabledHint && state.phase === "idle" ? (
          // Walkthrough 2026-07-07: this hint was 0.74rem text-faint — invisible
          // enough that a disabled Test button read as "the test button doesn't
          // work". A disabled control must SAY why, loudly (warn tone).
          <span className="font-ui text-[0.8rem] text-warn" role="status">
            {disabledHint}
          </span>
        ) : null}
      </span>
    </div>
  );
}
