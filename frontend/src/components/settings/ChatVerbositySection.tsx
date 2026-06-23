/**
 * Settings → Chat → Verbose Agent Chat (W-43).
 *
 * A simple on/off preference (default ON), persisted in localStorage via
 * `useVerboseAgentChat`. When OFF, the Build/Agent control pane collapses the
 * step-by-step Activity Feed to a compact stage card once the first plan is approved.
 * The gates (confirm/decision/question), the deliverable download/open panel, and the
 * full history (stage-card expand + the inspector's "Agent History" tab) all remain —
 * so this only quiets the running narrative, it never hides an affordance.
 */

import { MessageSquare, MessageSquareDashed } from "lucide-react";
import { cn } from "@/lib/cn";
import { useVerboseAgentChat } from "@/lib/useVerboseAgentChat";

export function ChatVerbositySection() {
  const { verbose, setVerbose } = useVerboseAgentChat();

  return (
    <section className="flex flex-col gap-section border-t border-hairline pt-section">
      <header>
        <h2 className="font-display text-[1.3rem] tracking-tight text-text">Agent chat</h2>
        <p className="mt-hair font-ui text-[0.86rem] text-text-muted">
          How much of the agent's step-by-step work shows in the control pane while it runs.
        </p>
      </header>

      <div className="flex flex-col gap-inline">
        {(
          [
            {
              on: true,
              Icon: MessageSquare,
              label: "Verbose (default)",
              help: "Show the full Activity Feed — every message, tool call, and output as it happens.",
            },
            {
              on: false,
              Icon: MessageSquareDashed,
              label: "Quiet",
              help: "Once the plan is approved, collapse the feed to a compact stage card (Planning / Reading / Building / …). Gates, downloads, and the full history (card expand + the inspector's “Agent History” tab) stay available.",
            },
          ] as const
        ).map(({ on, Icon, label, help }) => {
          const isActive = verbose === on;
          return (
            <button
              key={String(on)}
              type="button"
              data-disco-control="settings.verbose-chat-toggle"
              data-enabled={on}
              aria-pressed={isActive}
              onClick={() => {
                if (!isActive) setVerbose(on);
              }}
              className={cn(
                "flex items-start gap-inline rounded-card border px-body py-inline text-left transition-colors",
                isActive ? "border-accent/50 bg-accent/5" : "border-hairline hover:border-hairline-strong",
              )}
            >
              <Icon
                className={cn("mt-px size-4 shrink-0", isActive ? "text-accent" : "text-text-faint")}
                aria-hidden
              />
              <span className="flex min-w-0 flex-col gap-hair">
                <span className="font-ui text-[0.9rem] font-medium text-text">{label}</span>
                <span className="font-ui text-[0.8rem] leading-relaxed text-text-faint">{help}</span>
              </span>
            </button>
          );
        })}
      </div>
    </section>
  );
}
