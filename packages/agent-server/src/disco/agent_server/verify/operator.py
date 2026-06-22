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

import httpx
from websockets.asyncio.client import connect as _ws_connect

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
    ev = frame.get("event", frame)
    if ev.get("kind") == "status":
        return ev.get("status") or ev.get("execution_status")
    return None


class OperatorClient:
    """Watch + respond over the real HTTP/WS API."""

    def __init__(self, base_url: str = "http://127.0.0.1:8000") -> None:
        self.base = base_url.rstrip("/")
        self.ws_base = self.base.replace("http://", "ws://").replace("https://", "wss://")

    async def _events(self, cid: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(base_url=self.base, timeout=20.0) as hc:
            r = await hc.get(f"/conversations/{cid}/events")
            r.raise_for_status()
            d = r.json()
            return d if isinstance(d, list) else d.get("events", [])

    async def _status(self, cid: str) -> str | None:
        async with httpx.AsyncClient(base_url=self.base, timeout=20.0) as hc:
            r = await hc.get(f"/conversations/{cid}/state")
            if r.status_code != 200:
                return None
            d = r.json()
            return d.get("execution_status") or d.get("status")

    @staticmethod
    def _gate_context(status: str | None, events: list[dict[str, Any]]) -> dict[str, Any]:
        """Pull the operator-relevant context for the pending gate from the event log:
        the proposed plan, the question asked, or the offered alternatives."""
        ctx: dict[str, Any] = {}
        plan = None
        question = None
        alternatives = None
        for e in events:
            k = e.get("kind")
            # PlanEvent: kind="plan" with summary + steps (the proposal to approve).
            if k == "plan":
                plan = {
                    "summary": e.get("summary"),
                    "steps": e.get("steps"),
                    "revision": e.get("revision"),
                }
            # ActionEvent: the tool call lives under `tool_call` (name + arguments).
            tc = e.get("tool_call") or {}
            name = tc.get("name") or tc.get("tool_name")
            if name in ("ask_user", "clarify"):
                question = tc.get("arguments") or tc.get("args") or e.get("thought")
            if name in ("propose_alternatives", "offer_alternatives") or e.get("alternatives"):
                alternatives = e.get("alternatives") or tc.get("arguments")
            if k == "message" and e.get("source") == "agent":
                msg = e.get("message", {}).get("content")
                if msg:
                    ctx["last_agent_message"] = str(msg)[:1500]
        if status == "AWAITING_PLAN_APPROVAL" and plan:
            ctx["plan"] = plan
        if status == "AWAITING_USER_QUESTION" and question:
            ctx["question"] = question
        if status == "AWAITING_USER_DECISION" and alternatives:
            ctx["alternatives"] = alternatives
        return ctx

    @staticmethod
    def _derive_view(status: str | None, events: list[dict[str, Any]]) -> dict[str, Any]:
        """Assemble the FULL visible state of the surface from the event log — the same
        things a human sees with their eyes: the plan + its progress, the activity feed,
        the workspace files, terminal/server output, the rendered-page observation, the
        deliverables, the answer/sources, and the pending gate. Tool names live on the
        OBSERVATION (tool_result.tool_name); actions carry only args."""
        import re as _re

        plan: dict[str, Any] | None = None
        progress: Any = None
        activity: list[str] = []
        files: dict[str, int] = {}
        terminal: list[str] = []
        browser: dict[str, Any] | None = None
        deliverables: list[dict[str, Any]] = []
        messages: list[str] = []
        answer: str | None = None
        sources: list[Any] = []
        for e in events:
            k = e.get("kind")
            if k == "plan":
                plan = {"summary": e.get("summary"), "steps": e.get("steps")}
            elif k == "observation":
                tr = e.get("tool_result") or {}
                name = tr.get("tool_name")
                content = str(tr.get("content") or "")
                st = tr.get("structured") or {}
                activity.append(f"{name}: {content[:90]}" if content else f"{name}")
                if name == "file_write":
                    m = _re.search(r"wrote\s+(\d+)\s+bytes?\s+to\s+(.+)", content)
                    if m:
                        files[m.group(2).strip()] = int(m.group(1))
                elif name == "file_list" and isinstance(st.get("entries"), list):
                    for p in st["entries"]:
                        files.setdefault(str(p), 0)
                elif name in ("run_command", "bash", "exec", "server_status"):
                    terminal.append(content[:300])
                elif name == "browser":
                    browser = {kk: st.get(kk) for kk in ("url", "title", "console", "network")}
                elif name == "update_plan_progress":
                    progress = st.get("steps") or content
                elif name in ("research_answer", "answer"):
                    answer = content or answer
                    sources = st.get("sources") or st.get("citations") or sources
            elif k == "deliverable":
                deliverables.append({
                    "kind": e.get("artifact_kind"),
                    "path": e.get("path"),
                    "url": e.get("deployment_url"),
                })
            elif k == "message" and e.get("source") == "agent":
                msg = e.get("message", {}).get("content")
                if msg:
                    messages.append(str(msg)[:600])
        return {
            "status": status,
            "plan": plan,
            "plan_progress": progress,
            "activity": activity[-25:],
            "files": files,
            "terminal": terminal[-8:],
            "browser": browser,
            "deliverables": deliverables,
            "messages": messages[-4:],
            "answer": answer,
            "sources": sources[:8] if sources else [],
        }

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
        if view["files"].get("index.html") or any(d["kind"] == "app" for d in view["deliverables"]):
            try:
                async with httpx.AsyncClient(base_url=self.base, timeout=8.0) as hc:
                    r = await hc.get(f"/conversations/{cid}/preview-app/")
                    if r.status_code == 200 and r.content:
                        body = r.text
                        view["preview"] = {
                            "url": f"/conversations/{cid}/preview-app/",
                            "status": r.status_code,
                            "bytes": len(r.content),
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
        async with httpx.AsyncClient(base_url=self.base, timeout=20.0) as hc:
            r = await hc.get("/conversations")
            ids = (r.json() if r.status_code == 200 else {}).get("conversation_ids", [])
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

    async def start(
        self, surface: str, prompt: str, *, model: str | None = None
    ) -> dict[str, Any]:
        """Create a conversation on `surface` and submit the opening prompt — the operator
        kicking off a run (e.g. a build) it will then drive via wait/view/respond."""
        async with httpx.AsyncClient(base_url=self.base, timeout=30.0) as hc:
            r = await hc.post("/conversations", json={"surface": surface, "model_override": model})
            r.raise_for_status()
            cid = str(r.json()["conversation_id"])
        async with _ws_connect(f"{self.ws_base}/ws/conversations/{cid}") as ws:
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
            async with _ws_connect(url) as ws:
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
                # watch ALL conversations (GET /conversations → {"conversation_ids": [...]})
                async with httpx.AsyncClient(base_url=client.base, timeout=20.0) as hc:
                    r = await hc.get("/conversations")
                    body = r.json() if r.status_code == 200 else {}
                cids = [c for c in body.get("conversation_ids", []) if c]
            else:
                cids = [args.cid]
            return await client.wait(cids, timeout=args.timeout)
        return {"error": "no command"}

    out = asyncio.run(run())
    print(json.dumps(out, indent=2, default=str))
    return 0 if out.get("ok", True) and out.get("status") != "TIMEOUT" else 1


if __name__ == "__main__":
    sys.exit(main())
