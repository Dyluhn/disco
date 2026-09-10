import { useMemo } from "react";
import { Plus, Trash2 } from "lucide-react";
import type { WorkflowAuthoringContext, WorkflowMcpMountInput } from "@/types/workflow";

interface McpMountsFieldProps {
  mcpServers: WorkflowAuthoringContext["mcp_servers"];
  mcpMounts: WorkflowMcpMountInput[];
  emptyMcpMount: boolean;
  writableMcpNeedsPolicy: boolean;
  onAdd: (defaultServer: string | undefined) => void;
  onUpdateServer: (index: number, server: string) => void;
  onUpdateReadOnly: (index: number, readOnly: boolean) => void;
  onToggleTool: (index: number, tool: string, checked: boolean) => void;
  onRemove: (index: number) => void;
}

export function McpMountsField({
  mcpServers,
  mcpMounts,
  emptyMcpMount,
  writableMcpNeedsPolicy,
  onAdd,
  onUpdateServer,
  onUpdateReadOnly,
  onToggleTool,
  onRemove,
}: McpMountsFieldProps) {
  const mcpServerByName = useMemo(
    () => new Map(mcpServers.map((server) => [server.server, server])),
    [mcpServers],
  );

  if (mcpServers.length === 0) return null;

  return (
    <details className="rounded-control border border-hairline bg-surface-2 p-inline">
      <summary className="cursor-pointer font-ui text-[0.86rem] font-semibold text-text">
        MCP mounts
      </summary>
      <div className="mt-inline flex flex-col gap-inline">
        {mcpMounts.map((mount, index) => {
          const server = mcpServerByName.get(mount.server);
          return (
            <div key={index} className="flex flex-col gap-hair border-t border-hairline pt-inline">
              <div className="grid gap-hair md:grid-cols-[14rem_1fr_auto]">
                <select
                  aria-label={`MCP mount ${index + 1} server`}
                  value={mount.server}
                  onChange={(event) => onUpdateServer(index, event.target.value)}
                  className="min-h-11 rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.82rem] text-text outline-none focus:border-accent lg:min-h-0"
                >
                  {mcpServers.map((item) => (
                    <option key={item.server} value={item.server}>
                      {item.server}
                    </option>
                  ))}
                </select>
                <div className="flex flex-wrap gap-hair">
                  {(server?.tools ?? []).map((tool) => (
                    <label
                      key={tool}
                      className="flex items-center gap-hair rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.78rem] text-text-muted"
                    >
                      <input
                        type="checkbox"
                        checked={mount.tool_names.includes(tool)}
                        onChange={(event) => onToggleTool(index, tool, event.target.checked)}
                      />
                      {tool}
                    </label>
                  ))}
                </div>
                <button
                  type="button"
                  onClick={() => onRemove(index)}
                  aria-label={`Remove MCP mount ${index + 1}`}
                  className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-control border border-hairline px-inline py-hair text-text-muted hover:text-unsupported lg:min-h-0 lg:min-w-0"
                >
                  <Trash2 className="size-4" aria-hidden />
                </button>
              </div>
              <label className="flex items-center gap-hair font-ui text-[0.8rem] text-text-muted">
                <input
                  type="checkbox"
                  checked={mount.read_only}
                  onChange={(event) => onUpdateReadOnly(index, event.target.checked)}
                />
                Read-only mount
              </label>
            </div>
          );
        })}
        <button
          type="button"
          onClick={() => onAdd(mcpServers[0]?.server)}
          className="inline-flex min-h-11 w-fit items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted hover:text-text lg:min-h-0"
        >
          <Plus className="size-3.5" aria-hidden />
          Add MCP mount
        </button>
        {(emptyMcpMount || writableMcpNeedsPolicy) && (
          <div className="font-ui text-[0.78rem] text-unsupported">
            Pick at least one tool per mount; writable mounts require Allows writes.
          </div>
        )}
      </div>
    </details>
  );
}
