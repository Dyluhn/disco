"""Disco Operator — Claude as the live, non-deterministic operator of a Disco run.

This is the control plane that lets the OPERATOR (Claude, not a script) sit in the
human-in-the-loop seat: be told when a conversation needs something (a plan to
approve, a question to answer, a decision to make) or changes state, read the full
context, and RESPOND with judgment (approve / revise / answer / steer / pick / …).

It is the inverse of the disco-verify runner's canned ``auto_answer``: there the
harness sends a fixed reply; here a real intelligence decides each move.

Three verbs (all over the SAME HTTP/WS boundary the UI uses — nothing internal):

    python -m disco.agent_server.verify.operator state <cid>
        → current status + the pending gate's context (the proposed plan text, the
          question asked, the alternatives) as JSON.

    python -m disco.agent_server.verify.operator wait [<cid> | --any] [--timeout N]
        → BLOCK until any watched conversation reaches a gate or changes state, then
          print one JSON event {cid, status, gate, context} and exit. Run it in a
          loop (or as a backgrounded command that pings you on completion): wait →
          decide → respond → wait. This is the notifier.

    python -m disco.agent_server.verify.operator respond <cid> <action> [text]
        → make the response actionable. Actions:
            approve            approve the proposed plan
            revise   <text>    send the plan back with revision instructions
            reject             reject the pending risky action
            confirm            approve the pending risky action
            answer   <text>    answer an ask_user question
            steer    <text>    steer a running build mid-flight
            pick     <id>      choose an alternative
            resume | stop | pause | inject <text>

The respond verb keeps the socket open until the status actually moves off the gate
(or a short grace elapses), so the frame is never lost to a close-race.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any

from disco.core.auth import ISOLATED_PATH_PREVIEW_PREFIX
from websockets.asyncio.client import connect as _ws_connect

from .operator_parts import assemble_gate_context, fold_view_events, scan_gate_events
from .runner import AbstractVerifyClient, HttpVerifyClient

# A conversation will not advance off these without operator action.
_GATES: frozenset[str] = frozenset(
    {"AWAITING_PLAN_APPROVAL", "AWAITING_USER_QUESTION", "AWAITING_USER_DECISION"}
)
_TERMINAL: frozenset[str] = frozenset({"FINISHED", "ERROR", "STUCK", "IDLE"})

# operator action → the WS frame the UI sends (verified against frontend hooks).
_FRAME = {
    "approve": lambda t: {"type": "approve_plan"},
    "revise": lambda t: {"type": "request_plan", "content": t or ""},
    "reject": lambda t: {"type": "reject"},
    "confirm": lambda t: {"type": "confirm"},
    "answer": lambda t: {"type": "send_message", "content": t or ""},
    "send": lambda t: {"type": "send_message", "content": t or ""},
    "steer": lambda t: {"type": "steer", "steer_text": t or ""},
    "pick": lambda t: {"type": "pick_alternative", "option_id": t or ""},
    "resume": lambda t: {"type": "resume"},
    "stop": lambda t: {"type": "cancel"},
    "pause": lambda t: {"type": "pause"},
    "inject": lambda t: {"type": "inject_source", "inject_source_text": t or ""},
}


def _status_of(frame: dict[str, Any]) -> str | None:
    if frame.get("type") == "state":
        state = frame.get("state") or {}
        return state.get("execution_status") or state.get("status")
    ev = frame.get("event", frame)
    if ev.get("kind") == "status":
        return ev.get("status") or ev.get("execution_status")
    return None


class OperatorClient:
    """Watch + respond over the real HTTP/WS API."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        *,
        _auth_client: HttpVerifyClient | None = None,
        _preview_client: AbstractVerifyClient | None = None,
    ) -> None:
        self.base = base_url.rstrip("/")
        self.ws_base = self.base.replace("http://", "ws://").replace("https://", "wss://")
        self._auth_client = _auth_client or HttpVerifyClient(self.base)
        self._preview_client = _preview_client or self._auth_client

    async def _events(self, cid: str) -> list[dict[str, Any]]:
        r = await self._auth_client.auth.authenticated_request(
            "GET", f"/conversations/{cid}/events", timeout=20.0
        )
        r.raise_for_status()
        d = r.json()
        return d if isinstance(d, list) else d.get("events", [])

    async def _status(self, cid: str) -> str | None:
        r = await self._auth_client.auth.authenticated_request(
            "GET", f"/conversations/{cid}/state", timeout=20.0
        )
        if r.status_code != 200:
            return None
        d = r.json()
        return d.get("execution_status") or d.get("status")

    @staticmethod
    def _gate_context(status: str | None, events: list[dict[str, Any]]) -> dict[str, Any]:
        """Pull the operator-relevant context for the pending gate from the event log:
        the proposed plan, the question asked, or the offered alternatives."""
        plan, question, alternatives, ctx = scan_gate_events(events)
        return assemble_gate_context(status, plan, question, alternatives, ctx)

    @staticmethod
    def _derive_view(status: str | None, events: list[dict[str, Any]]) -> dict[str, Any]:
        """Assemble the FULL visible state of the surface from the event log — the same
        things a human sees with their eyes: the plan + its progress, the activity feed,
        the workspace files, terminal/server output, the rendered-page observation, the
        deliverables, the answer/sources, and the pending gate. Tool names live on the
        OBSERVATION (tool_result.tool_name); actions carry only args."""
        return fold_view_events(status, events)

    async def _has_live_preview_port(self, cid: str) -> bool:
        """Fix 2 (B-H.2): True iff the backend reports a live preview port for this
        conversation (a dev/http server is bound inside the sandbox). Hits the same
        GET /preview the UI polls (runtime.preview.preview() → available/ports). Best-effort:
        any failure ⇒ False (the probe is additive, never a hard gate)."""
        try:
            r = await self._auth_client.auth.authenticated_request(
                "GET", f"/conversations/{cid}/preview", timeout=6.0
            )
            if r.status_code != 200:
                return False
            d = r.json()
            return bool(d.get("available")) or bool(d.get("ports"))
        except Exception:  # noqa: BLE001 — additive probe; never block view()
            return False

    async def view(self, cid: str) -> dict[str, Any]:
        """The complete code-level visual of the conversation — what the operator would see
        with their eyes on the surface, plus the live preview body when there's a served app."""
        status = await self._status(cid)
        events = await self._events(cid)
        view = self._derive_view(status, events)
        view["cid"] = cid
        view["gate"] = status if status in _GATES else None
        if view["gate"]:
            view["gate_context"] = self._gate_context(status, events)
        # if the run served/produced a page, fetch what it actually looks like.
        # Fix 2 (B-H.2): ALSO probe when a live preview port is detected — a
        # shell-served site (`python3 -m http.server`) writes no index.html observation
        # and emits no app-deliverable, so the old gate left it invisible. The live
        # port (runtime.preview.preview() owners → available/ports) is the real signal that
        # there IS something to view.
        if (
            view["files"].get("index.html")
            or any(d["kind"] == "app" for d in view["deliverables"])
            or await self._has_live_preview_port(cid)
        ):
            try:
                fetched = await self._preview_client.fetch_preview(cid)
                if fetched is not None:
                    status, content = fetched
                    if status == 200 and content:
                        body = content.decode("utf-8", "replace")
                        view["preview"] = {
                            "url": f"{ISOLATED_PATH_PREVIEW_PREFIX}/{cid}/",
                            "status": status,
                            "bytes": len(content),
                            "is_directory_listing": "directory listing for" in body.lower(),
                            "html_head": body[:600],
                        }
            except Exception:  # noqa: BLE001
                view["preview"] = {"error": "preview not reachable"}
        return view

    async def list_conversations(
        self, *, active_only: bool = False, limit: int = 60
    ) -> dict[str, Any]:
        """Every conversation with its live status — so the orchestrator can SEE all running
        builds at once (GET /conversations only returns ids in an unreliable order, and there's
        no running-filter; this fills that gap). `active_only` keeps just the live ones."""
        ids = await self._conversation_ids()
        rows = await asyncio.gather(*(self._status(c) for c in ids[-limit:]))
        live = {"RUNNING", "PLANNING", *_GATES}
        out = [
            {"cid": c, "status": s}
            for c, s in zip(ids[-limit:], rows, strict=False)
            if not active_only or s in live
        ]
        return {
            "count": len(out),
            "active": sum(1 for o in out if o["status"] in live),
            "conversations": out,
        }

    async def _conversation_ids(self) -> list[str]:
        r = await self._auth_client.auth.authenticated_request("GET", "/conversations", timeout=20.0)
        body = r.json() if r.status_code == 200 else {}
        return [str(cid) for cid in body.get("conversation_ids", []) if cid]

    async def start(self, surface: str, prompt: str, *, model: str | None = None) -> dict[str, Any]:
        """Create a conversation on `surface` and submit the opening prompt — the operator
        kicking off a run (e.g. a build) it will then drive via wait/view/respond."""
        r = await self._auth_client.auth.authenticated_request(
            "POST",
            "/conversations",
            timeout=30.0,
            json={"surface": surface, "model_override": model},
        )
        r.raise_for_status()
        cid = str(r.json()["conversation_id"])
        origin, additional_headers = await self._auth_client.auth.websocket_credentials()
        async with _ws_connect(
            f"{self.ws_base}/ws/conversations/{cid}",
            origin=origin,
            additional_headers=additional_headers,
        ) as ws:
            await ws.send(json.dumps({"type": "send_message", "content": prompt}))
            try:  # brief grace so the server registers the message before we close
                await asyncio.wait_for(ws.recv(decode=True), timeout=5.0)
            except TimeoutError:
                pass
        return {"ok": True, "cid": cid, "surface": surface, "model": model}

    async def state(self, cid: str) -> dict[str, Any]:
        status = await self._status(cid)
        events = await self._events(cid)
        return {
            "cid": cid,
            "status": status,
            "at_gate": status in _GATES,
            "terminal": status in _TERMINAL,
            "gate": status if status in _GATES else None,
            "context": self._gate_context(status, events),
            "event_count": len(events),
        }

    async def wait(
        self, cids: list[str], *, timeout: float = 600.0, poll: float = 2.0
    ) -> dict[str, Any]:
        """Poll until ANY watched conversation reaches a gate or terminal, or a state
        change from the last seen. Returns the first such event."""
        deadline = time.monotonic() + timeout
        last: dict[str, str | None] = {}
        for cid in cids:
            last[cid] = await self._status(cid)
            if last[cid] in _GATES or last[cid] in _TERMINAL:
                return await self.state(cid)
        while time.monotonic() < deadline:
            await asyncio.sleep(poll)
            for cid in cids:
                st = await self._status(cid)
                if st != last[cid]:
                    last[cid] = st
                    if st in _GATES or st in _TERMINAL or st == "STUCK":
                        return await self.state(cid)
                    # a non-gate transition (PLANNING/RUNNING/…) — report it too
                    return {"cid": cid, "status": st, "at_gate": False, "transition": True}
        return {"status": "TIMEOUT", "watched": cids}

    async def respond(self, cid: str, action: str, text: str | None = None) -> dict[str, Any]:
        if action not in _FRAME:
            return {"ok": False, "error": f"unknown action {action!r}; valid: {sorted(_FRAME)}"}
        frame = _FRAME[action](text)
        before = await self._status(cid)
        url = f"{self.ws_base}/ws/conversations/{cid}"
        moved = before
        try:
            origin, additional_headers = await self._auth_client.auth.websocket_credentials()
            async with _ws_connect(
                url,
                origin=origin,
                additional_headers=additional_headers,
            ) as ws:
                await ws.send(json.dumps(frame))
                # keep the socket open until the status moves off the gate (or a grace
                # window) so the frame is never lost to a close-race.
                grace = time.monotonic() + 12.0
                while time.monotonic() < grace:
                    try:
                        raw = await asyncio.wait_for(ws.recv(decode=True), timeout=4.0)
                    except TimeoutError:
                        continue
                    st = _status_of(json.loads(raw))
                    if st and st != before:
                        moved = st
                        break
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "cid": cid, "action": action, "error": str(exc)}
        if moved == before:
            moved = await self._status(cid)
        return {
            "ok": True,
            "cid": cid,
            "action": action,
            "frame": frame,
            "status_before": before,
            "status_after": moved,
        }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Disco Operator — watch + respond to live runs.")
    ap.add_argument("--agent-base", default="http://127.0.0.1:8000")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list")
    p_list.add_argument("--active", action="store_true")
    p_start = sub.add_parser("start")
    p_start.add_argument("surface")
    p_start.add_argument("prompt")
    p_start.add_argument("--model", default=None)
    p_state = sub.add_parser("state")
    p_state.add_argument("cid")
    p_view = sub.add_parser("view")
    p_view.add_argument("cid")
    p_wait = sub.add_parser("wait")
    p_wait.add_argument("cid", nargs="?")
    p_wait.add_argument("--any", action="store_true")
    p_wait.add_argument("--timeout", type=float, default=600.0)
    p_resp = sub.add_parser("respond")
    p_resp.add_argument("cid")
    p_resp.add_argument("action")
    p_resp.add_argument("text", nargs="?")
    args = ap.parse_args(argv)
    client = OperatorClient(args.agent_base)

    async def run() -> dict[str, Any]:
        if args.cmd == "list":
            return await client.list_conversations(active_only=args.active)
        if args.cmd == "start":
            return await client.start(args.surface, args.prompt, model=args.model)
        if args.cmd == "state":
            return await client.state(args.cid)
        if args.cmd == "view":
            return await client.view(args.cid)
        if args.cmd == "respond":
            return await client.respond(args.cid, args.action, args.text)
        if args.cmd == "wait":
            if args.any or not args.cid:
                cids = await client._conversation_ids()
            else:
                cids = [args.cid]
            return await client.wait(cids, timeout=args.timeout)
        return {"error": "no command"}

    out = asyncio.run(run())
    print(json.dumps(out, indent=2, default=str))
    return 0 if out.get("ok", True) and out.get("status") != "TIMEOUT" else 1


if __name__ == "__main__":
    sys.exit(main())
