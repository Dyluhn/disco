/**
 * The MCP connections control — shown in the Agent composer's options menu.
 * One pill in the menus' shared control grammar (rounded-control, hairline
 * border, icon + short label): "Tools · N connected", deep-linking to Settings
 * where connections are managed. The per-server names live in the tooltip, not
 * as a chip strip — a strip of differently-shaped chips read as alien next to
 * the model pill and toggles. Read-only (useMcpConnections); never an
 * affordance that does nothing.
 */

import { Plug } from "lucide-react";
import { Link } from "react-router-dom";
import { useMcpConnections } from "@/hooks/useConfig";

export function ConnectionsStrip() {
  const { data: connections } = useMcpConnections();
  const servers = connections ?? [];
  // Only servers the user has enabled are reachable by the agent; show those.
  const usable = servers.filter((s) => s.enabled !== false);
  const connected = usable.filter((s) => s.status === "connected");
  const label =
    usable.length === 0 ? "Tools · none" : `Tools · ${connected.length} connected`;
  const detail =
    usable.length === 0
      ? "No tool servers connected — open Settings to add one"
      : usable.map((s) => `${s.name} — ${s.status}`).join(" · ");

  return (
    <Link
      to="/settings"
      title={detail}
      aria-label={`${label} — manage tool connections in Settings`}
      className="flex min-h-11 items-center gap-hair rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.76rem] text-text-muted transition-colors hover:border-hairline-strong hover:text-text lg:min-h-0"
    >
      <Plug className="size-3.5 shrink-0 text-text-faint" aria-hidden />
      {label}
    </Link>
  );
}
