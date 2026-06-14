import { useState } from "react";
import { Plus, Trash2 } from "lucide-react";
import { cn } from "@/lib/cn";
import {
  useApproveMcpServer,
  useCreateMcpServer,
  useDeleteMcpServer,
  useMcpConnections,
  useUpdateMcpServer,
} from "@/hooks/useConfig";
import type { McpServerConfig, McpStatus } from "@/types/config";

/**
 * MCP connections (rung B) — live add, toggle, approve, and remove external
 * tool servers. Backed by the app-server CRUD endpoints + the mcp_approvals
 * table. The NotWired banner and PendingBadge are removed — this surface is
 * now wired for real.
 */
const STATUS_META: Record<McpStatus, { label: string; dot: string }> = {
  connected: { label: "Connected", dot: "bg-supported" },
  disconnected: { label: "Disconnected", dot: "bg-text-faint" },
  error: { label: "Error", dot: "bg-unsupported" },
};

function Switch({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      onClick={() => onChange(!checked)}
      className={cn(
        "relative h-5 w-9 shrink-0 rounded-full border transition-colors",
        checked ? "border-accent/50 bg-accent/30" : "border-hairline bg-surface-2",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "absolute top-1/2 size-3.5 -translate-y-1/2 rounded-full transition-all",
          checked ? "left-[1.15rem] bg-accent" : "left-[0.15rem] bg-text-faint",
        )}
      />
    </button>
  );
}

interface DraftState {
  name: string;
  url: string;
  transport: string;
  risk_tier: string;
}

const EMPTY_DRAFT: DraftState = {
  name: "",
  url: "",
  transport: "streamable_http",
  risk_tier: "medium",
};

function ConnectionForm({
  initial,
  busy,
  onSave,
  onCancel,
}: {
  initial: DraftState;
  busy: boolean;
  onSave: (draft: DraftState) => void;
  onCancel: () => void;
}) {
  const [draft, setDraft] = useState<DraftState>(initial);
  const canSave = draft.name.trim().length > 0 && draft.url.trim().length > 0;
  return (
    <div className="flex flex-col gap-inline rounded-card border border-accent/40 bg-surface-1 p-body">
      <input
        value={draft.name}
        onChange={(e) => setDraft({ ...draft, name: e.target.value })}
        placeholder="Server name (e.g. filesystem)"
        aria-label="MCP server name"
        className="rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.88rem] text-text outline-none focus:border-accent"
      />
      <input
        value={draft.url}
        onChange={(e) => setDraft({ ...draft, url: e.target.value })}
        placeholder="URL or command (e.g. https://mcp.example.com)"
        aria-label="MCP server URL"
        className="rounded-control border border-hairline bg-surface-2 px-inline py-hair font-mono text-[0.82rem] text-text outline-none focus:border-accent"
      />
      <div className="flex items-center gap-inline">
        <label className="font-ui text-[0.78rem] text-text-muted">Transport:</label>
        <select
          value={draft.transport}
          onChange={(e) => setDraft({ ...draft, transport: e.target.value })}
          aria-label="Transport type"
          className="rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.78rem] text-text outline-none"
        >
          <option value="streamable_http">Streamable HTTP</option>
          <option value="stdio">Stdio</option>
        </select>
        <label className="font-ui text-[0.78rem] text-text-muted">Risk:</label>
        <select
          value={draft.risk_tier}
          onChange={(e) => setDraft({ ...draft, risk_tier: e.target.value })}
          aria-label="Risk tier"
          className="rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.78rem] text-text outline-none"
        >
          <option value="low">Low</option>
          <option value="medium">Medium</option>
          <option value="high">High</option>
          <option value="critical">Critical</option>
        </select>
      </div>
      <div className="flex items-center justify-end gap-inline">
        <button
          type="button"
          onClick={onCancel}
          className="rounded-control px-inline py-hair font-ui text-[0.8rem] text-text-muted hover:text-text"
        >
          Cancel
        </button>
        <button
          type="button"
          disabled={!canSave || busy}
          onClick={() => onSave(draft)}
          className="rounded-control bg-accent px-body py-hair font-ui text-[0.8rem] font-medium text-bg transition-opacity disabled:opacity-40"
        >
          {busy ? "Adding…" : "Add connection"}
        </button>
      </div>
    </div>
  );
}

function ApprovalDiff({
  name,
  oldHash,
  newHash,
  busy,
  onConfirm,
  onCancel,
}: {
  name: string;
  oldHash: string;
  newHash: string;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div
      role="alert"
      className="rounded-card border border-unsupported/40 bg-unsupported/5 p-body"
    >
      <p className="font-ui text-[0.82rem] font-medium text-unsupported">
        Tool descriptions changed — re-approval required
      </p>
      <p className="mt-px font-ui text-[0.76rem] text-text-muted">
        The tool set for <strong>{name}</strong> has changed since the last
        approval. Review the diff and confirm to accept the new tools.
      </p>
      <div className="mt-inline flex flex-col gap-hair font-mono text-[0.7rem]">
        <div className="rounded-control border border-hairline bg-surface-2 px-inline py-hair">
          <span className="text-text-faint">Old hash: </span>
          <span className="break-all text-text">{oldHash}</span>
        </div>
        <div className="rounded-control border border-hairline bg-surface-2 px-inline py-hair">
          <span className="text-text-faint">New hash: </span>
          <span className="break-all text-text">{newHash}</span>
        </div>
      </div>
      <div className="mt-inline flex items-center justify-end gap-inline">
        <button
          type="button"
          onClick={onCancel}
          className="rounded-control px-inline py-hair font-ui text-[0.78rem] text-text-muted hover:text-text"
        >
          Dismiss
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={onConfirm}
          className="rounded-control bg-accent px-body py-hair font-ui text-[0.78rem] font-medium text-bg transition-opacity disabled:opacity-40"
        >
          {busy ? "Approving…" : "Re-approve"}
        </button>
      </div>
    </div>
  );
}

export function McpSection() {
  const { data: connections, isLoading } = useMcpConnections();
  const create = useCreateMcpServer();
  const update = useUpdateMcpServer();
  const remove = useDeleteMcpServer();
  const approve = useApproveMcpServer();

  const [creating, setCreating] = useState(false);
  const [approvingServer, setApprovingServer] = useState<string | null>(null);

  const closeForm = () => setCreating(false);
  const closeApproval = () => setApprovingServer(null);

  const handleCreate = (draft: DraftState) => {
    const config: McpServerConfig = {
      name: draft.name,
      url: draft.url,
      transport: draft.transport,
      risk_tier: draft.risk_tier,
      enabled: true,
    };
    create.mutate(config, { onSuccess: closeForm });
  };

  const handleApprove = (serverName: string, hash: string) => {
    approve.mutate(
      { name: serverName, body: { description_hash: hash } },
      { onSuccess: closeApproval },
    );
  };

  return (
    <section aria-labelledby="mcp-heading" className="flex flex-col gap-inline">
      <div className="flex items-center justify-between gap-inline">
        <div className="flex items-center gap-inline">
          <h2
            id="mcp-heading"
            className="font-ui text-[1.05rem] font-semibold text-text"
          >
            Connections (MCP)
          </h2>
        </div>
        {!creating && (
          <button
            type="button"
            onClick={() => setCreating(true)}
            className="flex items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:border-accent hover:text-text"
          >
            <Plus className="size-3.5" aria-hidden />
            Add connection
          </button>
        )}
      </div>
      <p className="font-ui text-[0.84rem] text-text-muted">
        External tool servers. Add a connection to let the agent call tools from
        MCP-compatible servers. Approvals are required on first connect and when
        tool descriptions change.
      </p>

      {(create.error || update.error || remove.error || approve.error) && (
        <p role="alert" className="font-ui text-[0.8rem] text-unsupported">
          Couldn't save — the server didn't respond. Your connections are unchanged.
        </p>
      )}

      {creating && (
        <ConnectionForm
          initial={EMPTY_DRAFT}
          busy={create.isPending}
          onCancel={closeForm}
          onSave={handleCreate}
        />
      )}

      <ul className="overflow-hidden rounded-card border border-hairline bg-surface-1">
        {isLoading && (
          <li className="px-body py-body font-ui text-[0.84rem] text-text-muted">
            Loading connections…
          </li>
        )}
        {!isLoading &&
          (connections ?? []).length === 0 &&
          !creating && (
            <li className="px-body py-body text-center font-ui text-[0.84rem] text-text-faint">
              No connections yet. Add one to let the agent call external tools.
            </li>
          )}
        {(connections ?? []).map((c) => {
          const meta = STATUS_META[c.status as McpStatus] ?? {
            label: c.status,
            dot: "bg-text-faint",
          };
          const hashMismatch =
            approvingServer === c.id;
          return (
            <li
              key={c.id}
              className="flex flex-col border-b border-hairline last:border-b-0"
            >
              <div className="flex items-center justify-between gap-section px-body py-inline">
                <div className="min-w-0">
                  <div className="font-ui text-[0.88rem] font-medium text-text">
                    {c.name}
                  </div>
                  <p className="truncate font-mono text-[0.74rem] text-text-faint">
                    {c.url}
                  </p>
                  {c.transport && (
                    <span className="font-ui text-[0.68rem] text-text-faint">
                      {c.transport}
                      {c.risk_tier ? ` · ${c.risk_tier}` : ""}
                    </span>
                  )}
                  {c.description_hash && (
                    <p
                      className="mt-px truncate font-mono text-[0.65rem] text-text-faint"
                      title={c.description_hash}
                    >
                      {c.description_hash.slice(0, 12)}… (SHA-256)
                    </p>
                  )}
                </div>
                <div className="flex shrink-0 items-center gap-inline">
                  <span className="flex shrink-0 items-center gap-hair font-ui text-[0.76rem] text-text-muted">
                    <span
                      className={cn("size-1.5 rounded-full", meta.dot)}
                      aria-hidden
                    />
                    {meta.label}
                  </span>
                  {c.description_hash && c.new_description_hash && (
                    <button
                      type="button"
                      onClick={() => setApprovingServer(c.id)}
                      aria-label={`Re-approve ${c.name}`}
                      title="Re-approve changed tool descriptions"
                      className="text-text-faint transition-colors hover:text-accent font-ui text-[0.72rem]"
                    >
                      Re-approve
                    </button>
                  )}
                  <Switch
                    checked={c.enabled !== false}
                    onChange={(next) =>
                      update.mutate({
                        name: c.id,
                        patch: { name: c.id, url: c.url, enabled: next },
                      })
                    }
                    label={`Enable ${c.name}`}
                  />
                  <button
                    type="button"
                    onClick={() => remove.mutate(c.id)}
                    aria-label={`Remove ${c.name}`}
                    className="text-text-faint transition-colors hover:text-unsupported"
                  >
                    <Trash2 className="size-3.5" aria-hidden />
                  </button>
                </div>
              </div>
              {hashMismatch && c.description_hash && c.new_description_hash && (
                <div className="px-body pb-inline">
                  <ApprovalDiff
                    name={c.name}
                    oldHash={c.description_hash}
                    newHash={c.new_description_hash}
                    busy={approve.isPending}
                    onConfirm={() =>
                      handleApprove(c.id, c.new_description_hash!)
                    }
                    onCancel={closeApproval}
                  />
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
