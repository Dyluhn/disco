/**
 * The live agent-working view (BoD §13.4): the plan→act→observe trace as a structured,
 * quiet timeline — NOT a chat blob. Each action shows its tool + arguments + thought;
 * each observation its result; chroma appears only on meaningful elements (a failed
 * observation, a pending-gate action, the agent's final answer).
 */

import {
  Braces,
  FileText,
  Globe,
  Search,
  Terminal,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/cn";
import type { ActionEvent, AgentEvent, ObservationEvent } from "@/types/agent";

const TOOL: Record<string, { icon: LucideIcon; label: string }> = {
  shell: { icon: Terminal, label: "shell" },
  code_exec: { icon: Braces, label: "code" },
  file_write: { icon: FileText, label: "write" },
  file_read: { icon: FileText, label: "read" },
  file_edit: { icon: FileText, label: "edit" },
  file_list: { icon: FileText, label: "list" },
  browser: { icon: Globe, label: "browser" },
  search: { icon: Search, label: "search" },
  extract: { icon: Globe, label: "extract" },
};

function toolMeta(name: string) {
  return TOOL[name] ?? { icon: Wrench, label: name };
}

function argSummary(toolName: string, args: Record<string, unknown>): string {
  if (toolName === "shell") return String(args.command ?? "");
  if (toolName === "code_exec") return `${args.language ?? "python"} snippet`;
  if (toolName.startsWith("file_")) return String(args.path ?? "");
  if (toolName === "browser") return `${args.action ?? "navigate"} ${args.url ?? ""}`.trim();
  if (toolName === "search") return String(args.query ?? "");
  const s = JSON.stringify(args);
  return s.length > 80 ? s.slice(0, 80) + "…" : s;
}

function ActionRow({ event, pending }: { event: ActionEvent; pending: boolean }) {
  const tc = event.tool_call;
  const { icon: Icon, label } = toolMeta(tc?.tool_name ?? "");
  return (
    <li className="relative pl-7">
      <span
        className={cn(
          "absolute left-0 top-0.5 grid size-5 place-items-center rounded-control border",
          pending ? "border-warn text-warn" : "border-hairline text-text-muted",
        )}
      >
        <Icon className="size-3" aria-hidden />
      </span>
      <div className="flex flex-col gap-hair">
        {event.thought && (
          <p className="font-ui text-[0.82rem] leading-snug text-text-muted">{event.thought}</p>
        )}
        {tc && (
          <code className="block w-fit max-w-full overflow-x-auto rounded-control bg-surface-2 px-inline py-hair font-mono text-[0.78rem] text-text">
            <span className="text-text-faint">{label}&nbsp;</span>
            {argSummary(tc.tool_name, tc.arguments)}
          </code>
        )}
        {pending && (
          <span className="font-ui text-[0.72rem] uppercase tracking-wide text-warn">
            awaiting approval
          </span>
        )}
      </div>
    </li>
  );
}

function ObservationRow({ event }: { event: ObservationEvent }) {
  const r = event.tool_result;
  const body = (r.content || r.error || "(no output)").trim();
  return (
    <li className="relative pl-7">
      <span
        className={cn(
          "absolute left-2 top-1 size-1.5 rounded-full",
          r.success ? "bg-supported" : "bg-unsupported",
        )}
        aria-hidden
      />
      <pre
        className={cn(
          "max-h-40 overflow-y-auto whitespace-pre-wrap rounded-control border border-hairline bg-surface-1 px-inline py-hair font-mono text-[0.75rem] leading-snug",
          r.success ? "text-text-muted" : "text-unsupported",
        )}
      >
        {body.length > 600 ? body.slice(0, 600) + "\n…" : body}
      </pre>
    </li>
  );
}

export function AgentTrace({
  events,
  pendingActionId,
}: {
  events: AgentEvent[];
  pendingActionId: string | null;
}) {
  return (
    <ol className="flex flex-col gap-body">
      {events.map((e) => {
        switch (e.kind) {
          case "message":
            if (e.source === "agent")
              return (
                <li key={e.id} className="rounded-card border border-hairline bg-surface-1 px-body py-inline font-serif text-[0.98rem] leading-relaxed text-text">
                  {e.message.content}
                </li>
              );
            return null; // the user task is shown as the surface heading
          case "action":
            return <ActionRow key={e.id} event={e} pending={e.id === pendingActionId} />;
          case "observation":
            return <ObservationRow key={e.id} event={e} />;
          case "agent_error":
            return (
              <li key={e.id} className="pl-7 font-ui text-[0.8rem] text-unsupported">
                {e.error}
              </li>
            );
          case "condensation":
            return (
              <li key={e.id} className="flex items-center gap-inline py-hair">
                <span className="h-px flex-1 bg-hairline" />
                <span className="font-ui text-[0.7rem] uppercase tracking-wide text-text-faint">
                  memory condensed
                </span>
                <span className="h-px flex-1 bg-hairline" />
              </li>
            );
          default:
            return null; // status/error surfaced in the status bar, not the trace
        }
      })}
    </ol>
  );
}
