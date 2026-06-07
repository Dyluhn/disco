/**
 * The live activity narrator — what the agent is doing RIGHT NOW between
 * persisted events. Without token streaming, the static "Working" badge feels
 * frozen during slow local-model turns. This bar replaces it with a precise,
 * always-changing description ("Reading your message…", "Running `ls /tmp`…",
 * "Composing next step…") so the user can tell something is happening even
 * when the model takes 20 seconds to emit the next event.
 *
 * Pure derivation: lives.kind drives the icon + label. No animations beyond
 * the spinner — quiet in appearance, rich in function.
 */

import { Loader2, MessageSquare, Sparkles, Terminal, User } from "lucide-react";
import type { LiveSignal } from "@/lib/buildTrace";

export function LiveSignalBar({ signal }: { signal: LiveSignal }) {
  if (signal.kind === "idle") return null;

  const { Icon, label, detail } = describe(signal);
  return (
    <div className="flex items-center gap-hair rounded-control border border-dashed border-hairline bg-surface-1/60 px-inline py-hair">
      <Loader2 className="size-3.5 shrink-0 animate-spin text-accent" aria-hidden />
      <Icon className="size-3.5 shrink-0 text-text-faint" aria-hidden />
      <div className="flex min-w-0 flex-1 items-baseline gap-hair">
        <span className="font-ui text-[0.82rem] text-text">{label}</span>
        {detail && (
          <span className="min-w-0 truncate font-mono text-[0.74rem] text-text-faint">
            {detail}
          </span>
        )}
      </div>
    </div>
  );
}

function describe(signal: LiveSignal): {
  Icon: typeof Loader2;
  label: string;
  detail?: string;
} {
  switch (signal.kind) {
    case "starting":
      return { Icon: Sparkles, label: "Reading the goal…" };
    case "thinking_about_user_message":
      return {
        Icon: User,
        label: "Reading your message…",
        detail: signal.preview,
      };
    case "tool_executing": {
      const verb =
        signal.tool_name === "shell"
          ? "Running command"
          : signal.tool_name.startsWith("file_")
            ? signal.tool_name === "file_read"
              ? "Reading file"
              : signal.tool_name === "file_write"
                ? "Writing file"
                : signal.tool_name === "file_edit"
                  ? "Editing file"
                  : `Using ${signal.tool_name}`
            : signal.tool_name === "search"
              ? "Searching the web"
              : signal.tool_name === "extract"
                ? "Reading a web page"
                : `Calling ${signal.tool_name}`;
      return { Icon: Terminal, label: `${verb}…`, detail: signal.detail };
    }
    case "composing_next_step":
      return { Icon: MessageSquare, label: "Composing the next step…" };
    default:
      // unreachable — exhaustive switch
      return { Icon: Loader2, label: "Working…" };
  }
}
