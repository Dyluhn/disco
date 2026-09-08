"""Record policy-level environment reads without changing product decisions.

The research policy reads process-local cooldowns and clocks outside the search
provider. Provider-result recordings alone cannot replay those reads. These
scoped adapters preserve their inputs and observed values, including on turns
that only extract documents. They never restore or mutate the live registry.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from types import SimpleNamespace
from weakref import WeakKeyDictionary

from .cassette import Cassette, CassetteMiss

_VERSION = "research-environment-v1"
_META = "research.environment.schema"
_READ = "research.environment.read"
_ACTIVE: ContextVar[ResearchEnvironment | None] = ContextVar("harness_environment", default=None)
_RUNTIMES: WeakKeyDictionary = WeakKeyDictionary()
_LOCK = threading.RLock()
_USERS = 0
_RESTORES: list[tuple[object, str, object]] = []
_PREFIX = "disco.retrieval.deep_research."


class ResearchEnvironment:
    def __init__(self, cassette: Cassette, *, recording: bool):
        self.cassette = cassette
        self.recording = recording
        self.position = 0
        if recording:
            if _META in cassette.seams():
                raise ValueError("one research environment per recording cassette")
            cassette.record(_META, {}, {"version": _VERSION})
            self.enabled = True
        else:
            seams = cassette.seams()
            self.enabled = _META in seams
            if _READ in seams and not self.enabled:
                raise ValueError("environment reads lack their schema record")
            if self.enabled and seams[_META] != 1:
                raise ValueError("duplicate research environment schema")
            if self.enabled and cassette.lookup(_META, {}) != {"version": _VERSION}:
                raise ValueError("unsupported research environment recording")

    def _payload(self, name: str, arguments: dict) -> dict:
        result = {"position": self.position, "read": name, "arguments": arguments}
        self.position += 1
        return result

    def read(self, name: str, arguments: dict, live):
        payload = self._payload(name, arguments)
        if self.recording:
            value = json.loads(json.dumps(live(), allow_nan=False))
            self.cassette.record(_READ, payload, value)
            return value
        # Position AND call identity must match. An added/reordered read fails.
        return self.cassette.lookup(_READ, payload)

    async def sleep(self, name: str, seconds: float, live) -> None:
        payload = self._payload(name, {"seconds": seconds})
        if self.recording:
            await live(seconds)
            self.cassette.record(_READ, payload, None)
        else:
            if self.cassette.lookup(_READ, payload) is not None:
                raise ValueError("invalid recorded sleep result")
            # All policy clock reads are recorded. Preserve scheduling without
            # waiting out historical wall time in a deterministic replay.
            await asyncio.sleep(0)

    def assert_consumed(self) -> None:
        if not self.recording and self.enabled:
            expected = self.cassette.seams().get(_READ, 0)
            if self.position != expected:
                raise CassetteMiss(
                    f"environment reads left over: consumed={self.position}, recorded={expected}"
                )


def attach_environment(runtime, cassette: Cassette, *, recording: bool):
    _RUNTIMES[runtime] = ResearchEnvironment(cassette, recording=recording)
    return runtime


def _reader(name, live):
    def read():
        env = _ACTIVE.get()
        return live() if env is None else env.read(name, {}, live)

    return read


def _sleeper(name, live):
    async def sleep(seconds):
        env = _ACTIVE.get()
        if env is None:
            await live(seconds)
        else:
            await env.sleep(name, seconds, live)

    return sleep


def _clock_module(name: str, original):
    class DateTime(original.datetime):
        @classmethod
        def now(cls, tz=None):
            env = _ACTIVE.get()
            if env is None:
                return original.datetime.now(tz)
            value = env.read(
                name + ".datetime.now",
                {"timezone": str(tz)},
                lambda: original.datetime.now(tz).isoformat(),
            )
            return original.datetime.fromisoformat(value)

    class Date(original.date):
        @classmethod
        def today(cls):
            env = _ACTIVE.get()
            if env is None:
                return original.date.today()
            value = env.read(name + ".date.today", {}, lambda: original.date.today().isoformat())
            return original.date.fromisoformat(value)

    return SimpleNamespace(**{**vars(original), "datetime": DateTime, "date": Date})


def _replace(module, name, value):
    _RESTORES.append((module, name, getattr(module, name)))
    setattr(module, name, value)


def _install() -> None:
    reads = {
        "agent": ("engine_cooldown_seconds",),
        "_search_turn": ("engine_cooldown_seconds",),
        "_hold": ("engine_cooldown_seconds", "search_slot_available", "_now"),
    }
    for short, names in reads.items():
        module = importlib.import_module(_PREFIX + short)
        for name in names:
            _replace(module, name, _reader(short + "." + name, getattr(module, name)))
    hold = importlib.import_module(_PREFIX + "_hold")
    _replace(hold, "_sleep", _sleeper("_hold._sleep", hold._sleep))
    for short in (
        "agent",
        "writer",
        "decompose",
        "_search_outcomes",
        "_exhaustion",
        "_progress_events",
    ):
        module = importlib.import_module(_PREFIX + short)
        _replace(module, "datetime", _clock_module(short, module.datetime))


def _restore() -> None:
    while _RESTORES:
        module, name, original = _RESTORES.pop()
        setattr(module, name, original)


@contextmanager
def environment_scope(environment: ResearchEnvironment | None):
    """Context-local reads; unrelated tasks use live functions, even concurrently."""
    global _USERS
    if environment is None or not environment.enabled:
        token = _ACTIVE.set(None)
        try:
            yield
        finally:
            _ACTIVE.reset(token)
        return
    with _LOCK:
        if _USERS == 0:
            try:
                _install()
            except BaseException:
                _restore()
                raise
        _USERS += 1
    token = _ACTIVE.set(environment)
    try:
        yield
    except BaseException:
        raise
    else:
        environment.assert_consumed()
    finally:
        _ACTIVE.reset(token)
        with _LOCK:
            _USERS -= 1
            if _USERS == 0:
                _restore()


def runtime_environment(runtime):
    try:
        environment = _RUNTIMES.get(runtime)
    except TypeError:
        environment = None  # custom test builders need not support weak references
    return environment_scope(environment)
