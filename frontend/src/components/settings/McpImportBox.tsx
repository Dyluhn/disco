import { useState } from "react";
import { cn } from "@/lib/cn";
import { isApiFailure } from "@/api/errors";
import { useImportMcpConfig } from "@/hooks/useConfig";
import type { McpImportResult, McpImportServer } from "@/types/config";

/**
 * Paste-a-config for MCP connections. Accepts the standard `mcpServers` JSON
 * blob every MCP server README ships (stdio command+args+env, or url+headers),
 * previews the server(s) the app-server parsed out of it, and creates them
 * through the existing create path. Parsing happens ONLY on the server — this
 * box sends the raw text and renders what came back, including per-server
 * errors and the secret refs that will be stored for pasted literal values.
 */

function importErrorText(error: unknown): string {
  if (error instanceof Error && !isApiFailure(error)) return error.message;
  if (!isApiFailure(error)) return "Request failed.";
  try {
    const parsed = JSON.parse(error.message) as { detail?: unknown };
    return typeof parsed.detail === "string" ? parsed.detail : error.message;
  } catch {
    return error.message;
  }
}

function ServerPreviewRow({ server }: { server: McpImportServer }) {
  return (
    <li className="flex flex-col gap-hair border-b border-hairline px-inline py-hair last:border-b-0">
      <div className="flex items-center gap-inline">
        <span className="font-ui text-[0.84rem] font-medium text-text">
          {server.name}
        </span>
        {server.transport && (
          <span className="font-ui text-[0.68rem] text-text-faint">
            {server.transport}
          </span>
        )}
        {server.created && (
          <span className="font-ui text-[0.68rem] text-supported">Added</span>
        )}
      </div>
      {server.url && (
        <p className="truncate font-mono text-[0.72rem] text-text-faint">
          {server.url}
          {server.args?.length ? ` ${server.args.join(" ")}` : ""}
        </p>
      )}
      {server.error && (
        <p role="alert" className="font-ui text-[0.74rem] text-unsupported">
          {server.error}
        </p>
      )}
      {server.secrets.map((secret) => (
        <p key={secret.ref} className="font-ui text-[0.72rem] text-text-muted">
          {secret.already_configured ? "Updates" : "Stores"} secret{" "}
          <span className="font-mono">{secret.ref}</span> from the pasted{" "}
          <span className="font-mono">{secret.source_key}</span> value.
        </p>
      ))}
      {server.warnings.map((warning) => (
        <p key={warning} className="font-ui text-[0.72rem] text-text-muted">
          {warning}
        </p>
      ))}
    </li>
  );
}

export function McpImportBox({ onClose }: { onClose: () => void }) {
  const importConfig = useImportMcpConfig();
  const [text, setText] = useState("");
  const [preview, setPreview] = useState<McpImportResult | null>(null);

  const handle = (dryRun: boolean) => {
    importConfig.mutate(
      { text, dryRun },
      {
        onSuccess: (result) => {
          setPreview(result);
          if (!dryRun && result.servers.some((s) => s.created)) onClose();
        },
      },
    );
  };

  const addable = (preview?.servers ?? []).filter((s) => !s.error).length;
  const busy = importConfig.isPending;

  return (
    <div className="flex flex-col gap-inline rounded-card border border-accent/40 bg-surface-1 p-body">
      <p className="font-ui text-[0.78rem] text-text-muted">
        Paste the JSON config from an MCP server's README — the{" "}
        <span className="font-mono">mcpServers</span> shape, a bare server
        object, or the URL variant with headers. Pasted API keys are stored as
        secrets, never in the config.
      </p>
      <textarea
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          setPreview(null);
        }}
        data-disco-control="settings.mcp-import-text"
        aria-label="MCP config JSON"
        placeholder='{"mcpServers": {"name": {"command": "npx", "args": ["-y", "..."]}}}'
        rows={6}
        className="rounded-control border border-hairline bg-surface-2 px-inline py-hair font-mono text-[0.78rem] text-text outline-none focus:border-accent"
      />
      {importConfig.error != null && (
        <p role="alert" className="font-ui text-[0.78rem] text-unsupported">
          {importErrorText(importConfig.error)}
        </p>
      )}
      {preview && (
        <ul
          aria-label="Parsed MCP servers"
          className="overflow-hidden rounded-control border border-hairline bg-surface-2"
        >
          {preview.servers.map((server) => (
            <ServerPreviewRow key={server.name} server={server} />
          ))}
        </ul>
      )}
      <div className="flex items-center justify-end gap-inline">
        <button
          type="button"
          data-disco-control="settings.mcp-import-cancel"
          onClick={onClose}
          className="min-h-11 rounded-control px-inline py-hair font-ui text-[0.8rem] text-text-muted hover:text-text lg:min-h-0"
        >
          Cancel
        </button>
        <button
          type="button"
          data-disco-control="settings.mcp-import-preview"
          disabled={text.trim().length === 0 || busy}
          onClick={() => handle(true)}
          className="min-h-11 rounded-control border border-hairline px-inline py-hair font-ui text-[0.8rem] text-text-muted transition-colors hover:border-accent hover:text-text disabled:opacity-40 lg:min-h-0"
        >
          {busy ? "Working…" : "Preview"}
        </button>
        <button
          type="button"
          data-disco-control="settings.mcp-import-apply"
          disabled={!preview || preview.dry_run !== true || addable === 0 || busy}
          onClick={() => handle(false)}
          className={cn(
            "min-h-11 rounded-control bg-accent px-body py-hair font-ui text-[0.8rem] font-medium text-bg transition-opacity disabled:opacity-40 lg:min-h-0",
          )}
        >
          {busy
            ? "Adding…"
            : addable === 0
              ? "Add servers"
              : `Add ${addable} server${addable === 1 ? "" : "s"}`}
        </button>
      </div>
    </div>
  );
}
