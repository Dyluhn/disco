"""Disco messaging bridge — start and observe agent tasks from a chat channel.

A thin EXTERNAL process: it speaks the agent-server's PUBLIC REST surface to create a
task, kick it, poll it to completion, and pull the result — then a channel adapter
(Telegram) relays that to/from a person. It imports NOTHING from the Disco packages and
needs ZERO server-side changes; everything it uses is already exposed:

    POST /conversations                    create a task (pick the surface)
    POST /conversations/{cid}/messages     send the prompt → kicks the loop
    GET  /conversations/{cid}/state        poll execution_status until terminal
    GET  /conversations/{cid}/events       pull the result (report summary / answer)

Run it two ways:

    # one-shot (no chat channel — the verifiable path): run a task, print the result
    python disco_bot.py --base http://localhost:8000 --once "What is reciprocal rank fusion?"

    # Telegram bridge: relay messages from allow-listed chats to tasks and back
    python disco_bot.py --base http://localhost:8000 \
        --telegram-token "$DISCO_TELEGRAM_TOKEN" --allow 12345678

There is NO auth in Disco v1, so the bridge is the trust boundary: only chat ids in
`--allow` may start tasks, and each chat runs as its own owner (`tg:<chat_id>`), keeping
one person's conversations out of another's History.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

# execution_status values that mean "stop waiting". FINISHED/ERROR/STUCK are true
# terminals; the AWAITING_*/WAITING_* states mean the loop paused for a human decision
# the bridge cannot make — we report that honestly rather than hang forever.
_TERMINAL = frozenset({"FINISHED", "ERROR", "STUCK"})
_NEEDS_HUMAN = frozenset(
    {
        "WAITING_FOR_CONFIRMATION",
        "AWAITING_PLAN_APPROVAL",
        "AWAITING_USER_DECISION",
        "AWAITING_USER_QUESTION",
    }
)


@dataclass
class TaskResult:
    conversation_id: str
    status: str
    text: str
    url: str  # the in-app deep link a person can open to see the full run

    @property
    def ok(self) -> bool:
        return self.status == "FINISHED"


class DiscoClient:
    """The agent-server REST client the bridge drives. `http` is injected so tests run
    against an httpx.MockTransport with no network, and `sleep` is injected so the poll
    loop is instant under test."""

    def __init__(
        self,
        base_url: str,
        http: httpx.AsyncClient,
        *,
        owner_id: str = "local",
        app_base: str | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._http = http
        self._owner = owner_id
        # Where a person opens the run in the browser (the UI host), distinct from the
        # agent-server API base. Defaults to a relative path when unknown.
        self._app_base = (app_base or "").rstrip("/")
        self._sleep = sleep

    async def start_task(
        self, prompt: str, *, surface: str = "research", title: str | None = None
    ) -> str:
        """Create a conversation on `surface` and send the prompt (which kicks the loop).
        Returns the conversation id."""
        r = await self._http.post(
            f"{self._base}/conversations",
            json={"owner_id": self._owner, "surface": surface, "title": title or prompt[:60]},
        )
        r.raise_for_status()
        cid = r.json()["conversation_id"]
        m = await self._http.post(
            f"{self._base}/conversations/{cid}/messages", json={"content": prompt}
        )
        m.raise_for_status()
        return cid

    async def poll_until_done(
        self, cid: str, *, timeout_s: float = 900.0, interval_s: float = 4.0
    ) -> str:
        """Poll GET /state until the status is terminal (or needs a human, or we time
        out). Returns the final execution_status string (TIMEOUT if the budget elapses)."""
        waited = 0.0
        while waited <= timeout_s:
            r = await self._http.get(f"{self._base}/conversations/{cid}/state")
            r.raise_for_status()
            status = r.json().get("execution_status", "")
            if status in _TERMINAL or status in _NEEDS_HUMAN:
                return status
            await self._sleep(interval_s)
            waited += interval_s
        return "TIMEOUT"

    async def result_text(self, cid: str, *, limit: int = 400) -> str:
        """Best-effort extract of the human-facing result from the event log: a Deep
        Research report's executive summary, else the last assistant message."""
        r = await self._http.get(
            f"{self._base}/conversations/{cid}/events", params={"limit": limit}
        )
        r.raise_for_status()
        events = r.json().get("events", [])
        report_summary = None
        last_assistant = None
        for ev in events:  # ascending seq; keep the latest of each
            kind = ev.get("kind")
            if kind == "report" and ev.get("summary"):
                report_summary = ev["summary"]
            elif kind == "message":
                msg = ev.get("message") or {}
                if msg.get("role") == "assistant" and msg.get("content"):
                    last_assistant = msg["content"]
        return report_summary or last_assistant or "(no textual result — open the run to see it)"

    def url_for(self, cid: str, surface: str) -> str:
        # The UI routes a resume by surface: /deep/:cid, /build/:cid, /agent/:cid; a
        # plain research answer has no resume route, so link to History.
        route = {"deep_research": "deep", "build": "build", "agent": "agent"}.get(surface)
        path = f"/{route}/{cid}" if route else "/history"
        return f"{self._app_base}{path}" if self._app_base else path

    async def run(self, prompt: str, *, surface: str = "research", **poll_kw) -> TaskResult:
        """Start → wait → fetch. The one call a channel adapter needs."""
        cid = await self.start_task(prompt, surface=surface)
        status = await self.poll_until_done(cid, **poll_kw)
        url = self.url_for(cid, surface)
        if status == "FINISHED":
            text = await self.result_text(cid)
        elif status in _NEEDS_HUMAN:
            text = f"Paused — this task needs your input in the app ({status})."
        elif status == "TIMEOUT":
            text = "Still running — it's taking a while. Open the run to follow along."
        else:
            text = f"Task ended with status {status}."
        return TaskResult(conversation_id=cid, status=status, text=text, url=url)


def _format_reply(res: TaskResult) -> str:
    head = "✓ Done" if res.ok else f"• {res.status}"
    body = res.text if len(res.text) <= 1500 else res.text[:1500] + "…"
    return f"{head}\n\n{body}\n\n{res.url}"


class TelegramBridge:
    """Relays allow-listed Telegram chats ↔ Disco tasks via long-polling getUpdates.
    Each chat runs as its own owner so Histories don't mix. `http` is injected for
    tests; the real run uses a shared AsyncClient against api.telegram.org."""

    def __init__(
        self,
        token: str,
        base_url: str,
        http: httpx.AsyncClient,
        *,
        allow_chat_ids: set[int],
        surface: str = "research",
        app_base: str | None = None,
    ) -> None:
        self._api = f"https://api.telegram.org/bot{token}"
        self._http = http
        self._base = base_url
        self._allow = allow_chat_ids
        self._surface = surface
        self._app_base = app_base
        self._offset = 0

    async def _send(self, chat_id: int, text: str) -> None:
        await self._http.post(f"{self._api}/sendMessage", json={"chat_id": chat_id, "text": text})

    async def _handle(self, chat_id: int, text: str) -> None:
        if chat_id not in self._allow:
            return  # silent: an un-allowed chat is not a user we serve
        await self._send(chat_id, "Working on it…")
        client = DiscoClient(
            self._base, self._http, owner_id=f"tg:{chat_id}", app_base=self._app_base
        )
        try:
            res = await client.run(text, surface=self._surface)
            await self._send(chat_id, _format_reply(res))
        except Exception as exc:  # noqa: BLE001 — relay the real failure, don't swallow it
            await self._send(chat_id, f"Failed to run that: {type(exc).__name__}: {exc}")

    async def poll_once(self, *, timeout_s: int = 25) -> int:
        """One long-poll cycle: fetch updates, handle messages, advance the offset.
        Returns how many updates were processed (used by tests + the run loop)."""
        r = await self._http.get(
            f"{self._api}/getUpdates", params={"offset": self._offset, "timeout": timeout_s}
        )
        r.raise_for_status()
        updates = r.json().get("result", [])
        for u in updates:
            self._offset = max(self._offset, u["update_id"] + 1)
            msg = u.get("message") or {}
            text = msg.get("text")
            chat_id = (msg.get("chat") or {}).get("id")
            if text and chat_id is not None:
                await self._handle(int(chat_id), text)
        return len(updates)

    async def run_forever(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception as exc:  # noqa: BLE001 — a transient API blip shouldn't kill the bridge
                print(f"[disco-bot] poll error: {type(exc).__name__}: {exc}")
                await asyncio.sleep(3)


async def _amain(args: argparse.Namespace) -> int:
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as http:
        if args.once is not None:
            client = DiscoClient(args.base, http, owner_id=args.owner, app_base=args.app_base)
            try:
                res = await client.run(args.once, surface=args.surface)
            except httpx.HTTPError as exc:
                # A misconfigured --base / down server should read as a clear message,
                # not a stack trace.
                print(
                    f"Could not reach the agent-server at {args.base}: "
                    f"{type(exc).__name__}: {exc}"
                )
                return 1
            print(_format_reply(res))
            return 0 if res.ok else 1
        if not args.telegram_token:
            print("Nothing to do: pass --once <prompt> or --telegram-token <token>.")
            return 2
        bridge = TelegramBridge(
            args.telegram_token,
            args.base,
            http,
            allow_chat_ids={int(c) for c in args.allow},
            surface=args.surface,
            app_base=args.app_base,
        )
        print(
            f"[disco-bot] Telegram bridge up — surface={args.surface}, "
            f"allow={sorted(bridge._allow)}"
        )
        await bridge.run_forever()
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(prog="disco-bot", description=__doc__)
    ap.add_argument(
        "--base",
        default=os.environ.get("DISCO_AGENT_BASE", "http://localhost:8000"),
        help="agent-server base URL",
    )
    ap.add_argument(
        "--app-base",
        default=os.environ.get("DISCO_APP_BASE", ""),
        help="UI base URL for the deep links in replies (e.g. http://localhost:8088)",
    )
    ap.add_argument(
        "--surface", default="research", choices=["research", "deep_research", "build", "agent"]
    )
    ap.add_argument("--owner", default="local", help="owner_id for --once tasks")
    ap.add_argument(
        "--once", default=None, help="run ONE task with this prompt, print the result, exit"
    )
    ap.add_argument(
        "--telegram-token",
        default=os.environ.get("DISCO_TELEGRAM_TOKEN"),
        help="run the Telegram bridge with this bot token",
    )
    ap.add_argument("--allow", nargs="*", default=[], help="allow-listed Telegram chat ids")
    raise SystemExit(asyncio.run(_amain(ap.parse_args())))


if __name__ == "__main__":
    main()
