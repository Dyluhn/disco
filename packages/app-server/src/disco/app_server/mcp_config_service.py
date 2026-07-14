"""MCP settings and approval service extracted from ConfigState.

All persistence still flows through the shared ConfigStore and mcp approval DB
connection, while origin approval remains centralized in origin_approval_wiring.
ConfigState keeps its historical public/private methods as delegators.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from disco.core.llm import ConfigStore, SecretStore
from disco.core.llm.config import McpSettings

from . import origin_approval_wiring as _origin_wiring
from .config.dtos import (
    McpConnectionDTO,
    McpServerApproveDTO,
    McpServerConfigDTO,
    McpServerPatchDTO,
)
from .config.mappers import _mcp_live_status

if TYPE_CHECKING:
    # The shared mcp_approvals DB connection — a duck-typed sqlite3-like conn
    # (execute/commit). Type-only import keeps it off the runtime path.
    from disco.tools.mcp.migrations import _ApprovalConn


class McpConfigService:
    """MCP server CRUD and approval projection over the shared stores."""

    def __init__(
        self,
        store: ConfigStore,
        secrets: SecretStore,
        db_conn: _ApprovalConn | None,
    ) -> None:
        self._store = store
        self._secrets = secrets
        self._db_conn = db_conn

    def _mcp_config(self) -> McpSettings:
        return self._store.load().mcp

    def _mcp_approvals(self) -> dict[str, dict]:
        """Read all approval rows keyed by server name."""
        if self._db_conn is None:
            return {}
        try:
            from disco.tools.mcp.migrations import list_mcp_approvals

            rows = list_mcp_approvals(self._db_conn)
            return {r["server"]: r for r in rows}
        except Exception:
            return {}

    def _mcp_approval_pending(self) -> dict[str, dict]:
        """E6 (#10): read the per-server DRIFT rows the agent-server wrote.

        Each row is the AUTHORITATIVE new_hash the live agent-server pool
        computed at startup when it detected a description_hash mismatch
        against `mcp_approvals`. The app-server surfaces this on
        `McpConnectionDTO.new_description_hash` so the ApprovalDiff renders
        the REAL fingerprint of the changed tool set — not a stub of the
        stored hash. The agent-server clears the row in the same transaction
        as `create_mcp_approval` (the operator accepted the new tools), so
        `new_description_hash` is `None` once a server is in sync.
        """
        if self._db_conn is None:
            return {}
        try:
            from disco.tools.mcp.migrations import list_mcp_approval_pending

            rows = list_mcp_approval_pending(self._db_conn)
            return {r["server"]: r for r in rows}
        except Exception as exc:
            raise RuntimeError(
                "could not read MCP approval drift; refusing to project servers"
            ) from exc

    def _mcp_config_approvals(self) -> dict[str, dict]:
        if self._db_conn is None:
            return {}
        try:
            from disco.tools.mcp.migrations import list_mcp_config_approvals

            rows = list_mcp_config_approvals(self._db_conn)
            return {r["server"]: r for r in rows}
        except Exception:
            return {}

    def mcp_connections(self) -> list[McpConnectionDTO]:
        """Live pool projection: every configured server + its approval status.

        E6 (#10): also projects `new_description_hash` from the
        `mcp_approval_pending` drift table. When the live agent-server pool
        just computed a fresh hash that differs from the stored approval,
        `new_description_hash` carries that AUTHORITATIVE value and the
        frontend ApprovalDiff uses it for the new-hash side of the diff
        (and as the body of the re-approve POST). When the server is in
        sync (no drift row), `new_description_hash` is `None` and the UI
        does not show a diff banner.
        """
        cfg = self._mcp_config()
        approvals = self._mcp_approvals()
        config_approvals = self._mcp_config_approvals()
        pending = self._mcp_approval_pending()
        out: list[McpConnectionDTO] = []
        for name, srv in cfg.servers.items():
            url = (
                srv.get("url", "") or srv.get("command", [""])[0]
                if srv.get("command")
                else srv.get("url", "")
            )
            ap = approvals.get(name)
            cap = config_approvals.get(name)
            pd = pending.get(name)
            from disco.tools.mcp.approval import compute_config_hash

            current_config_hash = compute_config_hash({"name": name, **srv})
            config_pending = cap is None or cap["config_hash"] != current_config_hash
            out.append(
                McpConnectionDTO(
                    id=name,
                    name=name,
                    url=url,
                    status=(
                        "approval_required"
                        if config_pending or pd is not None or ap is None
                        else _mcp_live_status(ap)
                    ),
                    transport=srv.get("transport"),
                    risk_tier=srv.get("risk_tier"),
                    description_hash=ap["description_hash"] if ap else None,
                    # E6: pass the AUTHORITATIVE new_hash through verbatim —
                    # NEVER recompute here, the backend is the source of
                    # truth. None when the server is in sync.
                    new_description_hash=pd["new_hash"] if pd else None,
                    config_hash=cap["config_hash"] if cap else None,
                    new_config_hash=current_config_hash if config_pending else None,
                    approved_at=ap["approved_at"] if ap else None,
                    enabled=srv.get("enabled", True),
                )
            )
        return out

    def create_mcp_server(self, body: McpServerConfigDTO) -> McpConnectionDTO:
        cfg = self._mcp_config()
        if body.name in cfg.servers:
            raise ValueError(f"server {body.name!r} already exists")
        srv: dict = {
            "transport": body.transport,
            "url": body.url,
            "enabled": body.enabled,
            "allowed_tools": body.allowed_tools,
            "risk_tier": body.risk_tier,
        }
        if body.transport == "stdio":
            srv["command"] = [body.url]
        servers = {**cfg.servers, body.name: srv}
        # The Settings UI exposes per-connection switches, not a separate
        # top-level MCP switch.  A fresh install starts with ``mcp.enabled``
        # false, so persisting an enabled server without also activating MCP
        # leaves a connection that can be approved forever but can never be
        # started by the Agent runtime.  Treat the top-level bit as the
        # aggregate runtime gate for Settings-managed connections.
        new_cfg = cfg.model_copy(
            update={"servers": servers, "enabled": cfg.enabled or body.enabled}
        )
        self._store.save(self._store.load().model_copy(update={"mcp": new_cfg}))
        from disco.tools.mcp.approval import compute_config_hash

        config_hash = compute_config_hash({"name": body.name, **srv})
        return McpConnectionDTO(
            id=body.name,
            name=body.name,
            url=body.url,
            status="approval_required",
            transport=body.transport,
            risk_tier=body.risk_tier,
            enabled=body.enabled,
            new_config_hash=config_hash,
        )

    def update_mcp_server(self, name: str, patch: McpServerPatchDTO) -> McpConnectionDTO | None:
        cfg = self._mcp_config()
        if name not in cfg.servers:
            return None
        existing = cfg.servers[name]
        updated = {**existing}
        if patch.transport:
            updated["transport"] = patch.transport
        if patch.url:
            updated["url"] = patch.url
        if patch.enabled is not None:
            updated["enabled"] = patch.enabled
        if "allowed_tools" in patch.model_fields_set:
            updated["allowed_tools"] = patch.allowed_tools
        if patch.risk_tier:
            updated["risk_tier"] = patch.risk_tier
        servers = {**cfg.servers, name: updated}
        any_enabled = any(server.get("enabled", True) for server in servers.values())
        new_cfg = cfg.model_copy(update={"servers": servers, "enabled": any_enabled})
        self._store.save(self._store.load().model_copy(update={"mcp": new_cfg}))
        approvals = self._mcp_approvals()
        ap = approvals.get(name)
        config_approvals = self._mcp_config_approvals()
        cap = config_approvals.get(name)
        from disco.tools.mcp.approval import compute_config_hash

        current_config_hash = compute_config_hash({"name": name, **updated})
        config_pending = cap is None or cap["config_hash"] != current_config_hash
        return McpConnectionDTO(
            id=name,
            name=name,
            url=updated.get("url", ""),
            status="approval_required" if config_pending else _mcp_live_status(ap),
            transport=updated.get("transport"),
            risk_tier=updated.get("risk_tier"),
            description_hash=ap["description_hash"] if ap else None,
            approved_at=ap["approved_at"] if ap else None,
            enabled=updated.get("enabled", True),
            config_hash=cap["config_hash"] if cap else None,
            new_config_hash=current_config_hash if config_pending else None,
        )

    def delete_mcp_server(self, name: str) -> bool:
        cfg = self._mcp_config()
        if name not in cfg.servers:
            return False
        # Revoke durable approvals first. If revocation fails the configured
        # server remains present and cannot be recreated under stale hashes.
        if self._db_conn is not None:
            from disco.tools.mcp.migrations import (
                delete_mcp_approval,
                delete_mcp_config_approval,
            )

            delete_mcp_approval(self._db_conn, name)
            delete_mcp_config_approval(self._db_conn, name)
        servers = {k: v for k, v in cfg.servers.items() if k != name}
        any_enabled = any(server.get("enabled", True) for server in servers.values())
        new_cfg = cfg.model_copy(update={"servers": servers, "enabled": any_enabled})
        self._store.save(self._store.load().model_copy(update={"mcp": new_cfg}))
        return True

    def approve_mcp_server(self, name: str, body: McpServerApproveDTO) -> McpConnectionDTO:
        """Approve or re-approve — mutates the single row, never inserts a second."""
        cfg = self._mcp_config()
        if name not in cfg.servers:
            raise KeyError(f"unknown server {name!r}")
        if self._db_conn is None:
            raise RuntimeError("no DB connection for approval persistence")
        from disco.tools.mcp.approval import compute_config_hash
        from disco.tools.mcp.migrations import (
            create_mcp_approval,
            create_mcp_config_approval,
            get_mcp_approval_pending,
        )

        srv = cfg.servers[name]
        if body.approval_kind == "config":
            expected = compute_config_hash({"name": name, **srv})
            if body.config_hash != expected:
                raise ValueError("stale or missing MCP configuration hash")
            create_mcp_config_approval(self._db_conn, name, expected)
            _origin_wiring.approve_mcp_server_origin(self._store, self._secrets, name, srv)
        else:
            pending = get_mcp_approval_pending(self._db_conn, name)
            if (
                body.description_hash is None
                or pending is None
                or body.description_hash != pending["new_hash"]
            ):
                raise ValueError("stale or missing MCP tool-schema hash")
            create_mcp_approval(self._db_conn, name, body.description_hash)

        return next(c for c in self.mcp_connections() if c.id == name)

    def _mcp_secret_refs(self, srv: dict) -> tuple[str, ...]:
        return _origin_wiring.mcp_secret_refs(srv)

    def mcp_approval_diff(self, name: str, new_hash: str) -> dict | None:
        """Return old-vs-new hash diff for the approve UI. None = no diff or no
        stored approval."""
        if self._db_conn is None:
            return None
        from disco.tools.mcp.migrations import get_mcp_approval

        old = get_mcp_approval(self._db_conn, name)
        if old is None:
            return None
        old_hash = old["description_hash"]
        if old_hash == new_hash:
            return None
        return {
            "server": name,
            "old_hash": old_hash,
            "new_hash": new_hash,
            "approved_at": old["approved_at"],
            "approved_by": old["approved_by"],
        }
