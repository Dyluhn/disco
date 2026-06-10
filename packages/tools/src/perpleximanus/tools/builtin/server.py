from __future__ import annotations

from perpleximanus.core import SecurityRisk
from pydantic import BaseModel

from ..anatomy import Capability, ToolContext, ToolDef, ToolOutcome


class ServerStatusArgs(BaseModel):
    pass


class ServerStatusTool:
    definition = ToolDef(
        name="server_status",
        description="Show running background sessions and the ownership status of exposed ports (e.g. 8000).",
        args_model=ServerStatusArgs,
        needs=frozenset({Capability.SHELL}),
        base_risk=SecurityRisk.LOW,
        runs_in="sandbox",
        read_only=True,
    )

    async def run(self, args: ServerStatusArgs, ctx: ToolContext) -> ToolOutcome:
        assert ctx.sandbox is not None
        from ..sandbox.port_owner import port_owner
        
        # Get sessions
        sessions = await ctx.sandbox.sessions.list()
        
        out_lines = ["SERVER STATUS", "sessions:"]
        for s in sessions:
            state = "running" if s.busy else "idle"
            last = s.last_lines.split("\n")[-1] if s.last_lines else ""
            out_lines.append(f"  - {s.name}: {state} — last: {last}")
            
        if not sessions:
            out_lines.append("  (none)")
            
        out_lines.append("ports:")
        
        # Currently just 8000
        ports = [8000]
        prefix = f"pmx-{ctx.sandbox.sessions.namespace}" if ctx.sandbox.sessions.namespace else "pmx-"
        
        for p in ports:
            owner = await port_owner(ctx.sandbox, p)
            if owner is not None and owner.pid is not None:
                sess_name = owner.session
                if sess_name and sess_name.startswith(prefix):
                    sess_name = sess_name[len(prefix):]
                    
                sess_part = f" [session: {sess_name}]" if sess_name else " [session: null]"
                out_lines.append(f"  - {p}: OWNED by pid {owner.pid} ({owner.cmdline or ''}){sess_part}")
            else:
                out_lines.append(f"  - {p}: FREE")
                
        return ToolOutcome(
            success=True,
            content="\n".join(out_lines)
        )
