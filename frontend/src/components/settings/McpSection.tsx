import { useState } from "react";
import { Plus, ShieldOff, Trash2 } from "lucide-react";
import { cn } from "@/lib/cn";
import { agentLive, ApiError } from "@/api/client";
import { testMcpConnection } from "@/api/config";
import {
  useApproveMcpServer,
  useCreateMcpServer,
  useDeleteMcpServer,
  useMcpConnections,
  useRevokeMcpServer,
  useUpdateMcpServer,
} from "@/hooks/useConfig";
import type { McpServerConfig, McpStatus } from "@/types/config";
import { ProbeButton } from "./ProbeButton";

/**
 * MCP connections (rung B) — live add, toggle, approve, and remove external
 * tool servers. Backed by the app-server CRUD endpoints + the mcp_approvals
 * table. The NotWired banner and PendingBadge are removed — this surface is
 * now wired for real.
 */
const STATUS_META: Record<McpStatus, { label: string; dot: string }> = {
  disabled: { label: "Disabled", dot: "bg-text-faint" },
  connecting: { label: "Connecting", dot: "bg-accent" },
  connected: { label: "Connected", dot: "bg-supported" },
  degraded: { label: "Degraded", dot: "bg-unsupported" },
  disconnected: { label: "Disconnected", dot: "bg-text-faint" },
  error: { label: "Error", dot: "bg-unsupported" },
  approval_required: { label: "Approval required", dot: "bg-unsupported" },
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
      data-disco-control="settings.mcp-toggle"
      aria-checked={checked}
      aria-label={label}
      onClick={() => onChange(!checked)}
      className={cn(
        "relative h-5 w-9 shrink-0 rounded-full border transition-colors",
        checked
          ? "border-accent/50 bg-accent/30"
          : "border-hairline bg-surface-2",
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

function mcpErrorText(error: unknown): string {
  if (!(error instanceof ApiError)) return "Request failed.";
  try {
    const parsed = JSON.parse(error.message) as { detail?: unknown };
    return typeof parsed.detail === "string" ? parsed.detail : error.message;
  } catch {
    return error.message;
  }
}

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
        <label className="font-ui text-[0.78rem] text-text-muted">
          Transport:
        </label>
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
          data-disco-control="settings.mcp-add-save"
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
  kind = "tools",
}: {
  name: string;
  oldHash?: string;
  newHash: string;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  kind?: "config" | "tools";
}) {
  return (
    <div
      role="alert"
      className="rounded-card border border-unsupported/40 bg-unsupported/5 p-body"
    >
      <p className="font-ui text-[0.82rem] font-medium text-unsupported">
        {kind === "config"
          ? "Server configuration approval required"
          : "Tool schemas changed — approval required"}
      </p>
      <p className="mt-px font-ui text-[0.76rem] text-text-muted">
        Review the {kind === "config" ? "server configuration" : "tool schemas"} for{" "}
        <strong>{name}</strong> and confirm its fingerprint{" "}
        {kind === "config" ? "before Disco connects" : "before its tools are registered"}.
      </p>
      <div className="mt-inline flex flex-col gap-hair font-mono text-[0.7rem]">
        <div className="rounded-control border border-hairline bg-surface-2 px-inline py-hair">
          <span className="text-text-faint">Old hash: </span>
          <span className="break-all text-text">{oldHash || "Not previously approved"}</span>
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
          data-disco-control="settings.mcp-reapprove-confirm"
          disabled={busy}
          onClick={onConfirm}
          className="rounded-control bg-accent px-body py-hair font-ui text-[0.78rem] font-medium text-bg transition-opacity disabled:opacity-40"
        >
          {busy ? "Approving…" : "Approve"}
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
  const revoke = useRevokeMcpServer();

  const [creating, setCreating] = useState(false);
  const [approvingServer, setApprovingServer] = useState<string | null>(null);
  const mutationError =
    create.error ?? update.error ?? remove.error ?? approve.error ?? revoke.error;

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

  const handleApprove = (
    serverName: string,
    kind: "config" | "tools",
    hash: string,
  ) => {
    approve.mutate(
      {
        name: serverName,
        body:
          kind === "config"
            ? { approval_kind: "config", config_hash: hash }
            : { approval_kind: "tools", description_hash: hash },
      },
      { onSuccess: closeApproval },
    );
  };

  return (
    <section aria-labelledby="mcp-heading" className="flex flex-col gap-inline">
      <div className="flex items-center justify-between gap-inline">
        <div className="flex items-center gap-inline">
          <h3
            id="mcp-heading"
            className="font-ui text-[0.95rem] font-semibold text-text"
          >
            Connections (MCP)
          </h3>
        </div>
        {!creating && (
          <button
            type="button"
            data-disco-control="settings.mcp-add"
            disabled={isLoading}
            onClick={() => setCreating(true)}
            className="flex items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:border-accent hover:text-text disabled:cursor-wait disabled:opacity-40"
          >
            <Plus className="size-3.5" aria-hidden />
            {isLoading ? "Loading connections…" : "Add connection"}
          </button>
        )}
      </div>
      <p className="font-ui text-[0.84rem] text-text-muted">
        External tool servers. Add a connection to let the agent call tools from
        MCP-compatible servers. Approvals are required on first connect and when
        tool descriptions change.
      </p>

      {mutationError && (
        <p role="alert" className="font-ui text-[0.8rem] text-unsupported">
          Couldn't apply this change: {mcpErrorText(mutationError)}
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
        {!isLoading && (connections ?? []).length === 0 && !creating && (
          <li className="px-body py-body text-center font-ui text-[0.84rem] text-text-faint">
            No connections yet. Add one to let the agent call external tools.
          </li>
        )}
        {(connections ?? []).map((c) => {
          const meta = STATUS_META[c.status as McpStatus] ?? {
            label: c.status,
            dot: "bg-text-faint",
          };
          const hashMismatch = approvingServer === c.id;
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
                  <span
                    data-mcp-status={c.status}
                    className="flex shrink-0 items-center gap-hair font-ui text-[0.76rem] text-text-muted"
                  >
                    <span
                      className={cn("size-1.5 rounded-full", meta.dot)}
                      aria-hidden
                    />
                    {meta.label}
                  </span>
                  {(c.new_config_hash || c.new_description_hash) && (
                    <button
                      type="button"
                      data-disco-control="settings.mcp-reapprove"
                      onClick={() => setApprovingServer(c.id)}
                      aria-label={`Re-approve ${c.name}`}
                      title="Review required MCP approval"
                      className="text-text-faint transition-colors hover:text-accent font-ui text-[0.72rem]"
                    >
                      Review
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
                  {(c.config_hash || c.description_hash) && (
                    <button
                      type="button"
                      data-disco-control="settings.mcp-revoke"
                      onClick={() => revoke.mutate(c.id)}
                      aria-label={`Revoke approvals for ${c.name}`}
                      title="Revoke configuration, tool, and egress approvals"
                      className="text-text-faint transition-colors hover:text-unsupported"
                    >
                      <ShieldOff className="size-3.5" aria-hidden />
                    </button>
                  )}
                  <button
                    type="button"
                    data-disco-control="settings.mcp-remove"
                    onClick={() => remove.mutate(c.id)}
                    aria-label={`Remove ${c.name}`}
                    className="text-text-faint transition-colors hover:text-unsupported"
                  >
                    <Trash2 className="size-3.5" aria-hidden />
                  </button>
                </div>
              </div>
              {/* T4.5 live probe: handshake the server (initialize + tools/list)
                  and show the tool count or the real connection error. Disabled
                  until the agent-server (which owns the MCP transport) is up. */}
              <div className="px-body pb-inline">
                <ProbeButton
                  control="settings.mcp-test"
                  idleLabel="Test connection"
                  run={() => testMcpConnection(c.id)}
                  disabled={!agentLive()}
                  disabledHint="connect the agent server to test"
                />
              </div>
              {hashMismatch && (c.new_config_hash || c.new_description_hash) && (
                <div className="px-body pb-inline">
                  <ApprovalDiff
                    name={c.name}
                    oldHash={c.new_config_hash ? c.config_hash : c.description_hash}
                    newHash={c.new_config_hash ?? c.new_description_hash!}
                    kind={c.new_config_hash ? "config" : "tools"}
                    busy={approve.isPending}
                    onConfirm={() =>
                      handleApprove(
                        c.id,
                        c.new_config_hash ? "config" : "tools",
                        c.new_config_hash ?? c.new_description_hash!,
                      )
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
