/**
 * The MCP connections strip — shown on the Agent surface's hero. It's the single
 * loudest "this surface is for tools" signal: it surfaces the MCP servers the agent
 * can reach (a green dot for live ones) and a "Connect tools" deep-link to Settings.
 * MCP is the Agent surface's reason for being — general tools (a browser-driving
 * server, a database server, a SaaS server) only reach the build/agent executor, so
 * making them visible HERE is the point. Read-only (useMcpConnections); never an
 * affordance that does nothing.
 */

import { Plug, Plus } from "lucide-react";
import { Link } from "react-router-dom";
import { cn } from "@/lib/cn";
import { useMcpConnections } from "@/hooks/useConfig";

export function ConnectionsStrip() {
  const { data: connections } = useMcpConnections();
  const servers = connections ?? [];
  // Only servers the user has enabled are reachable by the agent; show those.
  const usable = servers.filter((s) => s.enabled !== false);

  // Same control grammar as the rest of the composer menus: rounded-control,
  // hairline border, 0.76rem UI text — not a differently-styled strip.
  return (
    <div className="flex flex-wrap items-center gap-hair">
      <span className="flex items-center gap-hair pr-hair font-ui text-[0.76rem] text-text-faint">
        <Plug className="size-3.5" aria-hidden /> Tools
      </span>
      {usable.map((s) => {
        const live = s.status === "connected";
        return (
          <span
            key={s.id}
            title={`${s.name} — ${s.status}`}
            className={cn(
              "flex items-center gap-hair rounded-control border px-inline py-hair font-ui text-[0.76rem]",
              live
                ? "border-supported/40 text-text"
                : s.status === "error"
                  ? "border-unsupported/40 text-text-muted"
                  : "border-hairline text-text-muted",
            )}
          >
            <span
              aria-hidden
              className={cn(
                "size-1.5 rounded-full",
                live ? "bg-supported" : s.status === "error" ? "bg-unsupported" : "bg-text-faint",
              )}
            />
            {s.name}
          </span>
        );
      })}
      <Link
        to="/settings"
        className="flex min-h-11 items-center gap-hair rounded-control border border-dashed border-hairline px-inline py-hair font-ui text-[0.76rem] text-text-muted transition-colors hover:border-accent hover:text-text lg:min-h-0"
      >
        <Plus className="size-3.5" aria-hidden />
        {usable.length === 0 ? "Connect tools" : "Add"}
      </Link>
    </div>
  );
}
