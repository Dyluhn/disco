import { useEffect, useRef, useState } from "react";

export interface DraftState {
  name: string;
  url: string;
  transport: string;
  risk_tier: string;
}

export const EMPTY_DRAFT: DraftState = {
  name: "",
  url: "",
  transport: "streamable_http",
  risk_tier: "medium",
};

export function ConnectionForm({
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
  const firstFieldRef = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    firstFieldRef.current?.focus();
  }, []);
  const canSave = draft.name.trim().length > 0 && draft.url.trim().length > 0;
  return (
    <div className="flex flex-col gap-inline rounded-card border border-accent/40 bg-surface-1 p-body">
      <input
        ref={firstFieldRef}
        value={draft.name}
        onChange={(e) => setDraft({ ...draft, name: e.target.value })}
        placeholder="Server name (e.g. filesystem)"
        aria-label="MCP server name"
        className="min-h-11 rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.88rem] text-text outline-none focus:border-accent lg:min-h-0"
      />
      <input
        value={draft.url}
        onChange={(e) => setDraft({ ...draft, url: e.target.value })}
        placeholder="URL or command (e.g. https://mcp.example.com)"
        aria-label="MCP server URL"
        className="min-h-11 rounded-control border border-hairline bg-surface-2 px-inline py-hair font-mono text-[0.82rem] text-text outline-none focus:border-accent lg:min-h-0"
      />
      <div className="flex items-center gap-inline">
        <label className="font-ui text-[0.78rem] text-text-muted">
          Transport:
        </label>
        <select
          value={draft.transport}
          onChange={(e) => setDraft({ ...draft, transport: e.target.value })}
          aria-label="Transport type"
          className="min-h-11 rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.78rem] text-text outline-none lg:min-h-0"
        >
          <option value="streamable_http">Streamable HTTP</option>
          <option value="stdio">Stdio</option>
        </select>
        <label className="font-ui text-[0.78rem] text-text-muted">Risk:</label>
        <select
          value={draft.risk_tier}
          onChange={(e) => setDraft({ ...draft, risk_tier: e.target.value })}
          aria-label="Risk tier"
          className="min-h-11 rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.78rem] text-text outline-none lg:min-h-0"
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
          className="min-h-11 rounded-control px-inline py-hair font-ui text-[0.8rem] text-text-muted hover:text-text lg:min-h-0"
        >
          Cancel
        </button>
        <button
          type="button"
          data-disco-control="settings.mcp-add-save"
          disabled={!canSave || busy}
          onClick={() => onSave(draft)}
          className="min-h-11 rounded-control bg-accent px-body py-hair font-ui text-[0.8rem] font-medium text-bg transition-opacity disabled:opacity-40 lg:min-h-0"
        >
          {busy ? "Adding…" : "Add connection"}
        </button>
      </div>
    </div>
  );
}

