import { Plus } from "lucide-react";
import { cn } from "@/lib/cn";
import { useMcpConnections } from "@/hooks/useConfig";
import type { McpStatus } from "@/types/config";
import { NotWired } from "./NotWired";
import { PendingBadge } from "./PendingBadge";

/**
 * MCP connections (Prompt 4) — add, view, and (eventually) configure external
 * tool servers. Scaffolded against fixtures and marked WIRING-PENDING: the MCP
 * client that actually dials these connections lands in a later phase, so the add
 * affordance is present but honestly inert for now.
 */
const STATUS_META: Record<McpStatus, { label: string; dot: string }> = {
  connected: { label: "Connected", dot: "bg-supported" },
  disconnected: { label: "Disconnected", dot: "bg-text-faint" },
  error: { label: "Error", dot: "bg-unsupported" },
};

export function McpSection() {
  const { data: connections, isLoading } = useMcpConnections();

  return (
    <section aria-labelledby="mcp-heading" className="flex flex-col gap-inline">
      <div className="flex items-center gap-inline">
        <h2 id="mcp-heading" className="font-ui text-[1.05rem] font-semibold text-text">
          Connections (MCP)
        </h2>
        <PendingBadge />
      </div>
      <p className="font-ui text-[0.84rem] text-text-muted">External tool servers.</p>
      <NotWired detail="The rows below are static placeholders, not real connections — there is no MCP client dialing anything, and 'Add connection' is disabled. (Needs: an MCP client in the agent-server plus persisted connection config.)" />

      <ul className="overflow-hidden rounded-card border border-hairline bg-surface-1">
        {isLoading && (
          <li className="px-body py-body font-ui text-[0.84rem] text-text-muted">
            Loading connections…
          </li>
        )}
        {(connections ?? []).map((c) => {
          const meta = STATUS_META[c.status];
          return (
            <li
              key={c.id}
              className="flex items-center justify-between gap-section border-b border-hairline px-body py-inline last:border-b-0"
            >
              <div className="min-w-0">
                <div className="font-ui text-[0.88rem] font-medium text-text">{c.name}</div>
                <p className="truncate font-mono text-[0.74rem] text-text-faint">{c.url}</p>
              </div>
              <span className="flex shrink-0 items-center gap-hair font-ui text-[0.76rem] text-text-muted">
                <span className={cn("size-1.5 rounded-full", meta.dot)} aria-hidden />
                {meta.label}
              </span>
            </li>
          );
        })}
        <li className="px-body py-inline">
          <button
            type="button"
            disabled
            title="Adding connections lands with the MCP client"
            className="flex items-center gap-hair rounded-control border border-dashed border-hairline px-body py-hair font-ui text-[0.8rem] text-text-faint"
          >
            <Plus className="size-3.5" aria-hidden />
            Add connection
          </button>
        </li>
      </ul>
    </section>
  );
}
