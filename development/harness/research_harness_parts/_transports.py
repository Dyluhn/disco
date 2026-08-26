"""Injectable transports: deterministic fake, cassette replay, and live wire.

A transport only has to return the ordered JSON frames it saw; the observer,
report writer, and invariants are shared by live, replay, and test-fake runs.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, cast

from ..cassette import Cassette
from ._deck import is_terminal as _deck_frame_is_terminal
from ._deck import summarize_frames
from ._observe import Observation, ResearchRequest, redact


class ResearchTransport(Protocol):
    async def collect(
        self, request: ResearchRequest, observer: Observation
    ) -> list[dict[str, Any]]: ...


class DeckCorrelationTransport(Protocol):
    async def correlate_deck(
        self, request: ResearchRequest, report: Mapping[str, Any]
    ) -> dict[str, Any] | None: ...


class FakeTransport:
    """Deterministic injectable transport used by unit tests and local probes."""

    def __init__(
        self,
        frames: Sequence[Mapping[str, Any]]
        | Callable[[ResearchRequest], Sequence[Mapping[str, Any]]]
        | Callable[[ResearchRequest], Awaitable[Sequence[Mapping[str, Any]]]],
        *,
        deck: Mapping[str, Any]
        | Callable[[ResearchRequest, Mapping[str, Any]], Mapping[str, Any] | None]
        | Callable[
            [ResearchRequest, Mapping[str, Any]],
            Awaitable[Mapping[str, Any] | None],
        ]
        | None = None,
    ) -> None:
        self._frames = frames
        self._deck = deck
        self.requests: list[ResearchRequest] = []

    async def collect(
        self, request: ResearchRequest, observer: Observation
    ) -> list[dict[str, Any]]:
        self.requests.append(request)
        frames: Any = self._frames(request) if callable(self._frames) else self._frames
        if hasattr(frames, "__await__"):
            frames = await cast(Awaitable[Sequence[Mapping[str, Any]]], frames)
        frames = cast(Sequence[Mapping[str, Any]], frames)
        result = [copy.deepcopy(dict(frame)) for frame in frames]
        for frame in result:
            observer.record(frame)
        return result

    async def correlate_deck(
        self, request: ResearchRequest, report: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        if self._deck is None:
            return None
        result: Any = (
            self._deck(request, report) if callable(self._deck) else copy.deepcopy(self._deck)
        )
        if hasattr(result, "__await__"):
            result = await cast(Awaitable[Mapping[str, Any] | None], result)
        return copy.deepcopy(dict(result)) if isinstance(result, Mapping) else None


class ReplayTransport:
    """Replay a captured research frame seam, with a cassette-compatible fallback.

    New captures should contain one ``research.frames`` row whose output is a
    list of wire frames.  Older harness cassettes only contain search/extract/
    LLM seams; the fallback projects those recorded responses into a small
    grounded final frame so old samples remain useful without network access.
    """

    def __init__(self, cassette: Cassette | str | Path) -> None:
        self.cassette = cassette if isinstance(cassette, Cassette) else Cassette.load(cassette)

    async def collect(
        self, request: ResearchRequest, observer: Observation
    ) -> list[dict[str, Any]]:
        rows = list(getattr(self.cassette, "_log", []))
        frames = self._frame_output(rows)
        if frames is None:
            frames = self._legacy_projection(rows, request)
            for row in rows:
                seam = str(row.get("seam") or "")
                if seam == "search":
                    payload = row.get("input") or {}
                    observer.record(
                        {
                            "type": "search",
                            "query": payload.get("query") if isinstance(payload, Mapping) else None,
                            "rows": len(row.get("output") or []),
                        },
                        kind="cassette",
                    )
                elif seam == "extract":
                    payload = row.get("input") or {}
                    output = row.get("output") or {}
                    observer.record(
                        {
                            "type": "extract",
                            "url": payload.get("url") if isinstance(payload, Mapping) else None,
                            "passage_count": len(output.get("passages") or [])
                            if isinstance(output, Mapping)
                            else 0,
                        },
                        kind="cassette",
                    )
        for frame in frames:
            observer.record(frame, kind="replay")
        # Restore diagnostic seams verbatim so replay preserves model I/O and
        # inspect evidence instead of recomputing only from public frames.
        model_io = self._seam_output(rows, "research.model_io")
        if model_io is not None:
            observer.model_io.clear()
            values = model_io if isinstance(model_io, list) else [model_io]
            for item in values:
                if isinstance(item, Mapping):
                    observer.record_model_io(item)
        provider_attempts = self._seam_output(rows, "research.provider_attempts")
        if isinstance(provider_attempts, list):
            observer.provider_attempts.clear()
            for item in provider_attempts:
                if isinstance(item, Mapping):
                    observer.record_provider_attempt(item)
        inspect_trace = self._seam_output(rows, "research.inspect")
        if isinstance(inspect_trace, Mapping):
            observer.record_inspect_trace(inspect_trace)
        return frames

    async def correlate_deck(
        self, request: ResearchRequest, report: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        output = self._seam_output(list(getattr(self.cassette, "_log", [])), "research.deck")
        return dict(output) if isinstance(output, Mapping) else None

    @staticmethod
    def _seam_output(rows: Sequence[Mapping[str, Any]], seam: str) -> Any:
        for row in rows:
            if row.get("seam") == seam:
                return row.get("output")
        return None

    @staticmethod
    def _frame_output(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]] | None:
        names = {"research.frames", "research.frame", "ws.research", "deep_research.frames"}
        for row in rows:
            if row.get("seam") not in names:
                continue
            output = row.get("output")
            if isinstance(output, Mapping) and isinstance(output.get("frames"), list):
                output = output["frames"]
            if isinstance(output, list) and all(isinstance(item, Mapping) for item in output):
                return [dict(item) for item in output]
        return None

    @staticmethod
    def _legacy_rows(
        rows: Sequence[Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
        """Fold a legacy cassette's seams into recorded hits/passages/answer."""
        hits: list[dict[str, Any]] = []
        passages: list[dict[str, Any]] = []
        answer_text = ""
        for row in rows:
            if row.get("seam") == "search" and isinstance(row.get("output"), list):
                hits.extend(dict(x) for x in row["output"] if isinstance(x, Mapping))
            elif row.get("seam") == "extract" and isinstance(row.get("output"), Mapping):
                passages.extend(
                    dict(x)
                    for x in (row["output"].get("passages") or [])
                    if isinstance(x, Mapping)
                )
            elif row.get("seam") == "llm.complete" and isinstance(row.get("output"), Mapping):
                answer_text = answer_text or str(row["output"].get("text") or "")
        return hits, passages, answer_text

    @classmethod
    def _legacy_projection(
        cls, rows: Sequence[Mapping[str, Any]], request: ResearchRequest
    ) -> list[dict[str, Any]]:
        """Project a legacy (search/extract/LLM seam) cassette without fabrication.

        Older cassettes carry no report structure.  The projection surfaces only
        what was actually recorded — search hits, extracted passages, and any
        recorded answer text.  It never manufactures a summary, sections, or
        citations: a legacy capture that lacks a real report must fail the
        report invariants honestly rather than being repaired into a passing
        shape.  ``legacy_replay`` marks the report so the summary output can say
        why the structure is missing.
        """
        hits, passages, answer_text = cls._legacy_rows(rows)
        report: dict[str, Any] = {
            "query": request.query,
            "passages": passages,
            "all_hits": hits,
            "depth_tier": request.depth,
            "legacy_replay": True,
        }
        if answer_text:
            report["answer_markdown"] = answer_text
        return [
            {"type": "state", "status": "running"},
            {"type": "phase", "phase": "search", "query": request.query},
            {"type": "final", "answer": report},
            {"type": "state", "status": "finished"},
        ]


class LiveWebSocketTransport:
    """Use the deployed agent-server wire API, with no product-side hooks."""

    def __init__(self, base_url: str | None = None, *, watch: bool = False) -> None:
        self.base_url = (base_url or "http://localhost:8000").rstrip("/")
        self.watch = watch
        self.conversation_id: str | None = None

    def _record(
        self,
        frames: list[dict[str, Any]],
        observer: Observation,
        frame: Mapping[str, Any],
    ) -> None:
        payload = dict(frame)
        frames.append(payload)
        observer.record(payload, kind="live")
        if not self.watch:
            return
        event: Mapping[str, Any] = payload
        if isinstance(payload.get("event"), Mapping):
            event = cast(Mapping[str, Any], payload["event"])
        label = str(event.get("kind") or payload.get("type") or "frame")
        status = _conversation_status(payload)
        suffix = f" status={status}" if status else ""
        observed = observer.events[-1]
        print(
            f"[{observed.elapsed_ms:>7} ms] {observed.phase:<12} {label}{suffix}",
            file=sys.stderr,
            flush=True,
        )

    async def _session_credentials(
        self, request: ResearchRequest
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Establish the same cookie/CSRF session used by the browser.

        ``auth_token`` is an optional Disco pairing token, not an HTTP bearer
        credential.  When it is omitted, a loopback server can supply its own
        pairing token through the public loopback-only convenience route.
        Neither the token nor the resulting cookie enters observable frames.
        """
        import httpx

        origin = self.base_url
        cookies = httpx.Cookies()
        async with httpx.AsyncClient(
            base_url=self.base_url,
            cookies=cookies,
            headers={"Origin": origin, "Accept": "application/json"},
            timeout=request.timeout_s,
        ) as client:
            response = await client.get("/api/auth/session")
            response.raise_for_status()
            body = response.json()
            if not body.get("authenticated"):
                pairing_token = request.auth_token
                minted = await client.post(
                    "/api/auth/mint",
                    json={"pairing_token": pairing_token} if pairing_token else {},
                )
                if minted.status_code == 401 and not pairing_token:
                    pairing = await client.get("/api/auth/pairing-token")
                    pairing.raise_for_status()
                    pairing_token = str(pairing.json().get("pairing_token") or "")
                    if not pairing_token:
                        raise RuntimeError("agent-server pairing route returned no token")
                    minted = await client.post(
                        "/api/auth/mint", json={"pairing_token": pairing_token}
                    )
                minted.raise_for_status()
                body = minted.json()
            cookies.update(client.cookies)
        csrf = str(body.get("csrf_token") or "")
        session_cookie = cookies.get("disco_session")
        if not csrf or not session_cookie:
            raise RuntimeError("agent-server pairing did not establish a session")
        return (
            {
                "Origin": origin,
                "Cookie": f"disco_session={session_cookie}",
            },
            {"X-Disco-CSRF": csrf},
        )

    async def collect(
        self, request: ResearchRequest, observer: Observation
    ) -> list[dict[str, Any]]:
        if request.surface == "research":
            return await self._research_socket(request, observer)
        return await self._conversation_socket(request, observer)

    async def correlate_deck(
        self, request: ResearchRequest, report: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """Start and observe the server-owned report → Agent deck handoff.

        This is deliberately a follow-up transport operation.  The product
        route remains the authority for resolving the report and creating the
        target Agent conversation; the harness only observes its public HTTP
        response and WebSocket lifecycle.
        """
        source_id = self.conversation_id
        base: dict[str, Any] = {
            "requested": True,
            "source_conversation_id": source_id,
            "surface": "agent",
            "contract": "deck",
        }
        if not source_id:
            return {**base, "ok": False, "error": "deck_requires_deep_research_conversation"}
        import httpx
        import websockets

        session_headers, mutation_headers = await self._session_credentials(request)
        origin = session_headers.pop("Origin")
        timeout = request.deck_timeout_s or 1200.0
        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers={**session_headers, **mutation_headers, "Origin": origin},
            timeout=timeout,
        ) as client:
            try:
                response = await client.post(
                    f"/api/conversations/{source_id}/report/deck",
                )
                body = response.json()
            except Exception as exc:  # network/control failure is a deck diagnostic
                return {**base, "ok": False, "error": _safe_error(exc)}
        if response.status_code >= 400 or not isinstance(body, Mapping):
            return {
                **base,
                "ok": False,
                "http_status": response.status_code,
                "error": _safe_error(body if isinstance(body, Mapping) else response.text),
            }
        target_id = str(body.get("conversation_id") or body.get("job_id") or "")
        if not target_id:
            return {**base, "ok": False, "error": "deck_handoff_missing_target_id"}
        base.update(
            {
                "target_conversation_id": target_id,
                "job_id": str(body.get("job_id") or target_id),
                "source_event_id": str(body.get("source_event_id") or ""),
                "format": str(body.get("format") or "pptx"),
            }
        )
        url = self.base_url.replace("https://", "wss://").replace("http://", "ws://")
        frames: list[dict[str, Any]] = []
        elapsed: list[int] = []
        max_observed_frames = 512
        started = time.monotonic()
        try:
            async with _connect(
                websockets,
                f"{url}/ws/conversations/{target_id}",
                session_headers,
                timeout,
                origin=origin,
            ) as ws:
                initial_raw = await asyncio.wait_for(
                    ws.recv(), timeout=min(timeout, 10.0)
                )
                initial = json.loads(initial_raw)
                if not isinstance(initial, Mapping):
                    raise RuntimeError("deck WebSocket did not return a state frame")
                frames.append(dict(initial))
                elapsed.append(round((time.monotonic() - started) * 1000))
                if not _deck_frame_is_terminal(initial):
                    async with asyncio.timeout(timeout):
                        async for raw in ws:
                            frame = json.loads(raw)
                            if not isinstance(frame, Mapping):
                                continue
                            frames.append(dict(frame))
                            elapsed.append(round((time.monotonic() - started) * 1000))
                            if len(frames) > max_observed_frames:
                                frames.pop(0)
                                elapsed.pop(0)
                            if _deck_frame_is_terminal(frame):
                                break
        except Exception as exc:
            diagnostic = summarize_frames(frames, elapsed_ms=elapsed)
            return {**base, **diagnostic, "ok": False, "error": _safe_error(exc)}
        diagnostic = summarize_frames(frames, elapsed_ms=elapsed)
        terminal = str(diagnostic.get("terminal_status") or "")
        validation = diagnostic.get("artifact_validation")
        typed_ok = isinstance(validation, Mapping) and bool(validation.get("typed_slides"))
        native_ok = isinstance(validation, Mapping) and bool(validation.get("native_pptx"))
        return {
            **base,
            **diagnostic,
            "ok": terminal == "FINISHED"
            and typed_ok
            and native_ok
            and not bool(validation.get("degraded"))
            if isinstance(validation, Mapping)
            else False,
        }

    async def _research_socket(
        self, request: ResearchRequest, observer: Observation
    ) -> list[dict[str, Any]]:
        import websockets

        url = self.base_url.replace("https://", "wss://").replace("http://", "ws://")
        session_headers, _ = await self._session_credentials(request)
        origin = session_headers.pop("Origin")
        frames: list[dict[str, Any]] = []
        async with _connect(
            websockets,
            f"{url}/ws/research",
            session_headers,
            request.timeout_s,
            origin=origin,
        ) as ws:
            await ws.send(json.dumps(request.wire_body()))
            async with asyncio.timeout(request.timeout_s):
                async for raw in ws:
                    frame = json.loads(raw)
                    self._record(frames, observer, frame)
                    if frame.get("type") == "error":
                        break
                    if _is_terminal_research_frame(frame):
                        break
        return frames

    async def _conversation_socket(
        self, request: ResearchRequest, observer: Observation
    ) -> list[dict[str, Any]]:
        import httpx
        import websockets

        session_headers, mutation_headers = await self._session_credentials(request)
        origin = session_headers.pop("Origin")
        body = {
            "surface": "deep_research",
            "title": request.query[:100],
            "model_override": request.model,
            "depth_tier": request.depth,
            "recency_window": request.recency,
            "sources": [request.provider] if request.provider else [],
            "autonomous": True,
        }
        body = {key: value for key, value in body.items() if value is not None}
        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers={**session_headers, **mutation_headers, "Origin": origin},
            timeout=request.timeout_s,
        ) as client:
            response = await client.post("/conversations", json=body)
            response.raise_for_status()
            conversation_id = str(response.json()["conversation_id"])
        self.conversation_id = conversation_id
        url = self.base_url.replace("https://", "wss://").replace("http://", "ws://")
        frames: list[dict[str, Any]] = []
        try:
            async with _connect(
                websockets,
                f"{url}/ws/conversations/{conversation_id}",
                session_headers,
                request.timeout_s,
                origin=origin,
            ) as ws:
                initial_raw = await asyncio.wait_for(
                    ws.recv(), timeout=min(request.timeout_s or 10.0, 10.0)
                )
                initial = json.loads(initial_raw)
                if not isinstance(initial, dict) or initial.get("type") != "state":
                    raise RuntimeError("conversation WebSocket did not begin with a state frame")
                self._record(frames, observer, initial)
                await ws.send(json.dumps({"type": "send_message", "content": request.query}))
                self._record(
                    frames,
                    observer,
                    {"type": "harness_control", "command": "send_message"},
                )
                # v2 (PKG-35): deep research is gateless — the question launches the
                # run and the model's brief streams as its first output; there is no
                # AWAITING_PLAN_APPROVAL state to acknowledge.
                async with asyncio.timeout(request.timeout_s):
                    async for raw in ws:
                        frame = json.loads(raw)
                        self._record(frames, observer, frame)
                        nested_event = frame.get("event")
                        event = nested_event if isinstance(nested_event, Mapping) else frame
                        if event.get("kind") == "report":
                            # The report is terminal for a deep-research run; keep
                            # reading one status event when the server supplies it.
                            continue
                        if event.get("kind") == "research_checkpoint":
                            break
                        if _conversation_status(frame).upper() in {"FINISHED", "ERROR", "PAUSED"}:
                            break
                        if frame.get("type") == "error":
                            break
        finally:
            # Inspect is diagnostic-only. Fetch it after normal completion,
            # timeout, or provider failure so an in-flight ``started`` attempt
            # survives a harness timeout. The helper swallows only its own
            # diagnostics; the original collection exception remains active.
            if request.capture_inspect:
                await self._collect_inspect(
                    request,
                    observer,
                    session_headers=session_headers,
                    origin=origin,
                    conversation_id=conversation_id,
                )
        return frames

    async def _collect_inspect(
        self,
        request: ResearchRequest,
        observer: Observation,
        *,
        session_headers: Mapping[str, str],
        origin: str,
        conversation_id: str,
    ) -> None:
        """Best-effort inspect fetch that cannot replace a transport failure."""
        try:
            import httpx

            async with httpx.AsyncClient(
                base_url=self.base_url,
                headers={**session_headers, "Origin": origin},
                timeout=request.timeout_s,
            ) as client:
                evidence = await client.get(f"/api/debug/evidence/{conversation_id}")
                if evidence.is_success:
                    snapshot = evidence.json()
                    if isinstance(snapshot, Mapping):
                        observer.record_inspect_trace(snapshot)
                        trace = snapshot.get("inspect_trace")
                        if isinstance(trace, Mapping):
                            for item in trace.get("model_io", []):
                                if isinstance(item, Mapping):
                                    observer.record_model_io(item)
        except BaseException as exc:  # noqa: BLE001 — diagnostics must not mask collection
            try:
                observer.record({"type": "harness_diagnostic", "inspect_error": str(exc)})
            except Exception:
                pass


def _transport_for(request: ResearchRequest) -> ResearchTransport:
    if request.cassette:
        return ReplayTransport(request.cassette)
    return LiveWebSocketTransport(request.base_url)


def _conversation_status(frame: Mapping[str, Any]) -> str:
    if frame.get("type") == "state" and isinstance(frame.get("state"), Mapping):
        return str(frame["state"].get("execution_status") or "")
    if frame.get("type") == "event" and isinstance(frame.get("event"), Mapping):
        event = frame["event"]
        if event.get("kind") == "status":
            return str(event.get("status") or "")
    return ""


def _is_terminal_research_frame(frame: Mapping[str, Any]) -> bool:
    """Recognize terminal report, checkpoint, and lifecycle frames."""
    nested_event = frame.get("event")
    event: Mapping[str, Any] = nested_event if isinstance(nested_event, Mapping) else frame
    if str(event.get("kind") or "").lower() in {"report", "research_checkpoint"}:
        return True
    status = str(event.get("status") or "").upper()
    if frame.get("type") == "state":
        state = frame.get("state")
        state_status = state.get("execution_status") if isinstance(state, Mapping) else None
        status = str(frame.get("status") or state_status or status).upper()
    return status in {"FINISHED", "ERROR", "PAUSED"}


def _connect(
    websockets: Any,
    url: str,
    headers: Mapping[str, str],
    timeout: float | None,
    *,
    origin: str | None = None,
) -> Any:
    kwargs = {"open_timeout": timeout, "origin": origin}
    if headers:
        kwargs["additional_headers"] = dict(headers)
    try:
        return websockets.connect(url, **kwargs)
    except TypeError:
        kwargs.pop("additional_headers", None)
        if headers:
            kwargs["extra_headers"] = dict(headers)
        return websockets.connect(url, **kwargs)


def _safe_error(value: Any) -> str:
    """Keep control errors useful without allowing credentials into artifacts."""
    if isinstance(value, Mapping):
        value = value.get("detail") or value.get("message") or value.get("error") or value
    return str(redact(str(value)))[:500]
