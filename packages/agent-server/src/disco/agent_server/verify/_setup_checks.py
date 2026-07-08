"""`disco verify` — does YOUR configured model actually drive the loop?

The strategic wedge of disco is *reliability on local / open-weight models*.
The corollary is that a self-hoster needs to find out their specific model works
**before** trusting it with a task — not discover mid-run that it can't emit a tool
call. This command runs a small battery of capability checks against the persisted
config + live endpoints and prints a pass/fail table:

    config       — a driver model is assigned and has a base URL
    completion   — the driver endpoint is reachable and returns text
    tool-calling — the driver emits a structured tool call when asked (THE gate:
                   a model that can't tool-call cannot drive the agent loop)
    grounding    — the full research pipeline returns a cited, NLI-verified answer
                   (network + encoder download required; SKIPs cleanly if offline)

Each check is PASS / FAIL / SKIP with the real reason (a provider's verbatim error,
never a flattened one). The process exits non-zero iff any check FAILED (a SKIP is
not a failure — it means "couldn't run here", e.g. no internet for grounding).

Run it:
    # in a compose deployment
    docker compose exec agent-server python -m disco.agent_server.verify
    # from a checkout
    uv run python -m disco.agent_server.verify [--quick] [--no-network]

This reuses the SAME router/provider/grounding composition the live loop uses
(ConversationRuntime._router_now, retrieval.wiring.research_answer) — so a pass here
is evidence about the real pipeline, not a toy.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass
from typing import Literal

from disco.core.events import LLMMessage
from disco.core.llm.types import (
    CapabilityProfile,
    CompletionRequest,
    ModelRole,
    Requirement,
    ToolSpec,
)

Status = Literal["PASS", "FAIL", "SKIP"]


@dataclass
class Check:
    name: str
    status: Status
    detail: str
    ms: int | None = None


# A trivial, unambiguous tool the model should call when asked about the weather.
# Structured-tool-call support is the single capability the whole agent loop rests
# on, so we test it directly rather than inferring it from a prose answer.
_WEATHER_TOOL = ToolSpec(
    name="get_weather",
    description="Get the current weather for a city. Call this to answer weather questions.",
    parameters_schema={
        "type": "object",
        "properties": {"city": {"type": "string", "description": "City name"}},
        "required": ["city"],
    },
)


def _profile() -> CapabilityProfile:
    return CapabilityProfile(
        role=ModelRole.AGENT_DRIVER,
        requirements=frozenset({Requirement.TOOL_CALLING}),
    )


def _build_runtime():
    """A throwaway runtime bound to the persisted config + secrets (the same stores
    the servers use). We only borrow its router builder — no event store writes.

    ConfigStore()/SecretStore() with no args resolve PMX_CONFIG / PMX_SECRETS (set by
    compose) and otherwise fall back to their own defaults — the repo
    `disco-config.json` for config, the XDG path for secrets — so verify reads
    exactly what the running servers read."""
    from disco.agent_server import ConversationRuntime
    from disco.core import SqliteEventStore
    from disco.core.llm import ConfigStore, SecretStore

    return ConversationRuntime(
        SqliteEventStore(":memory:"),
        config_store=ConfigStore(),
        secret_store=SecretStore(),
    )


def check_config(rt) -> Check:
    """A driver model must RESOLVE — by explicit AGENT_DRIVER assignment OR the
    `default_model` fallback (the same order the router uses, routing.py:216). This
    catches the most common first-run mistake: a config with no usable driver at all."""
    try:
        cfg = rt._config_store.load()
        driver_key = (
            cfg.assignments.get(ModelRole.AGENT_DRIVER.value)
            or cfg.assignments.get(ModelRole.AGENT_DRIVER)
            or cfg.default_model
        )
        if not driver_key:
            return Check("config", "FAIL", "no driver: set a default_model or assign AGENT_DRIVER")
        model = cfg.models.get(driver_key)
        if model is None:
            return Check("config", "FAIL", f"driver '{driver_key}' is not in the model catalogue")
        if not model.base_url:
            return Check(
                "config",
                "FAIL",
                (
                    "driver model is not configured: add an OpenAI-compatible "
                    "endpoint in Settings -> Models & Providers, set it as the "
                    "Default primary, then re-run disco-verify"
                ),
            )
        where = model.base_url or "(provider default)"
        return Check("config", "PASS", f"driver '{model.model_id}' → {where}")
    except Exception as exc:  # noqa: BLE001 — report the real error to the operator
        return Check("config", "FAIL", f"{type(exc).__name__}: {exc}")


async def check_completion(rt) -> Check:
    """The driver endpoint is reachable and returns text. Distinguishes 'endpoint down /
    wrong base_url / model not served' (FAIL with the verbatim error) from a working model."""
    try:
        router = rt._router_now()
        req = CompletionRequest(
            profile=_profile(),
            messages=[LLMMessage(role="user", content="Reply with exactly the word: ok")],
            # Budget must fit a reasoning model's think pass AND a short answer. A tiny
            # cap (the old 16) is spent entirely in `reasoning_content` by any reasoning
            # driver (MiniMax M3, DeepSeek V4, Qwen3.x …), which returns empty `text` —
            # a false "endpoint dead" verdict. Give it room to think and still answer.
            max_tokens=2048,
        )
        t0 = time.monotonic()
        resp = await router.complete(req)
        ms = int((time.monotonic() - t0) * 1000)
        # A live endpoint proves itself three ways: answer text, a tool call, OR hitting
        # the token budget mid-generation (finish_reason="length" — the reasoning-model
        # case where the think pass consumed the budget before the answer surfaced).
        alive = (
            bool((resp.text or "").strip())
            or bool(resp.tool_calls)
            or resp.finish_reason == "length"
        )
        if not alive:
            return Check("completion", "FAIL", f"empty response from '{resp.model_used}'", ms)
        return Check("completion", "PASS", f"'{resp.model_used}' replied in {ms} ms", ms)
    except Exception as exc:  # noqa: BLE001
        return Check("completion", "FAIL", f"{type(exc).__name__}: {exc}")


async def check_tool_calling(rt) -> Check:
    """THE gate. A model that won't emit a structured tool call cannot drive the loop —
    no matter how fluent its prose. We force the simplest possible call and check for it."""
    try:
        router = rt._router_now()
        req = CompletionRequest(
            profile=_profile(),
            messages=[
                LLMMessage(role="system", content="You are an assistant with tools. Use them."),
                LLMMessage(
                    role="user",
                    content="What is the weather in Paris right now? Use the get_weather tool.",
                ),
            ],
            tools=[_WEATHER_TOOL],
        )
        resp = await router.complete(req)
        names = [tc.tool_name for tc in resp.tool_calls]
        if "get_weather" in names:
            return Check("tool-calling", "PASS", f"'{resp.model_used}' called get_weather")
        if names:
            return Check("tool-calling", "FAIL", f"called {names}, expected get_weather")
        preview = (resp.text or "").strip().replace("\n", " ")[:80]
        return Check(
            "tool-calling",
            "FAIL",
            f"'{resp.model_used}' answered in prose, no tool call — “{preview}”",
        )
    except Exception as exc:  # noqa: BLE001
        return Check("tool-calling", "FAIL", f"{type(exc).__name__}: {exc}")


async def check_grounding(rt) -> Check:
    """The full research pipeline (live search + extraction + the user's driver + NLI
    verification) returns a CITED answer. This is a capability smoke test, not a quality
    gate — the faithfulness SCORE is the eval harness's job (`make eval`). Needs outbound
    internet + a one-time encoder download, so it SKIPs (not FAILs) when those aren't
    available — the point is to verify a *configured* model, not to penalize an offline box.

    Semantics: no sources retrieved → SKIP (search blocked / rate-limited — environmental);
    sources retrieved + ≥1 grounded claim → PASS; sources but zero grounded claims → FAIL
    (a real "your model retrieves but doesn't ground" gap worth surfacing)."""
    try:
        from disco.retrieval.bundled_providers import (
            DdgsSearchProvider,
            LocalExtractionProvider,
        )
        from disco.retrieval.engine import DefaultRetrievalEngine
        from disco.retrieval.grounding import GroundingPipeline
        from disco.retrieval.live import build_live_retrieval
        from disco.retrieval.ranking import RouterQueryRewriter
        from disco.retrieval.wiring import research_answer
    except Exception as exc:  # noqa: BLE001
        return Check("grounding", "SKIP", f"retrieval extras unavailable: {exc}")

    try:
        enc = build_live_retrieval()  # encoders (downloads on first run)
        router = rt._router_now()
        engine = DefaultRetrievalEngine(
            search=DdgsSearchProvider(),
            extraction=LocalExtractionProvider(),
            reranker=enc["reranker"],
            embedder=enc.get("embedder"),
            rewriter=RouterQueryRewriter(router),
        )
        grounding = GroundingPipeline(router, enc["nli"])
        answer = await research_answer(
            "Who wrote the novel Pride and Prejudice?", engine, grounding, depth="standard"
        )
        cited = {p.source_url for p in answer.passages}
        n_claims = len(answer.claims)
        if not cited:
            return Check(
                "grounding", "SKIP", "no sources retrieved (search blocked / rate-limited)"
            )
        if n_claims > 0:
            return Check(
                "grounding", "PASS", f"cited answer: {len(cited)} source(s), {n_claims} claim(s)"
            )
        return Check(
            "grounding", "FAIL", f"retrieved {len(cited)} source(s) but produced 0 grounded claims"
        )
    except Exception as exc:  # noqa: BLE001
        # Network / encoder-download / search failures are environmental, not a model
        # verdict — SKIP with the reason so the operator knows what to fix.
        return Check("grounding", "SKIP", f"could not run live ({type(exc).__name__}: {exc})")


async def run_checks(*, quick: bool, network: bool) -> list[Check]:
    rt = _build_runtime()
    results: list[Check] = [check_config(rt)]
    # If config failed, the live checks can't resolve a model — SKIP them honestly.
    if results[0].status == "FAIL":
        for name in ("completion", "tool-calling", "grounding"):
            results.append(Check(name, "SKIP", "skipped — config check failed"))
        return results

    results.append(await check_completion(rt))
    results.append(await check_tool_calling(rt))
    if quick or not network:
        results.append(Check("grounding", "SKIP", "skipped (--quick / --no-network)"))
    else:
        results.append(await check_grounding(rt))
    return results


def _render(results: list[Check]) -> int:
    icons = {"PASS": "✓", "FAIL": "✗", "SKIP": "–"}
    width = max(len(c.name) for c in results)
    print("\ndisco — setup verification\n")
    for c in results:
        print(f"  {icons[c.status]} {c.status:<4} {c.name.ljust(width)}   {c.detail}")
    failed = [c for c in results if c.status == "FAIL"]
    passed = sum(1 for c in results if c.status == "PASS")
    skipped = sum(1 for c in results if c.status == "SKIP")
    print(f"\n  {passed} passed · {len(failed)} failed · {skipped} skipped")
    if failed:
        print("\n  Setup is NOT ready. Fix the failing checks above, then re-run.")
        return 1
    print("\n  Your model is ready to drive the loop.")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="disco verify",
        description="Verify your configured model/provider actually drives the agent loop.",
    )
    ap.add_argument(
        "--quick", action="store_true", help="skip the live grounding check (config + LLM only)"
    )
    ap.add_argument(
        "--no-network",
        action="store_true",
        help="skip checks that need outbound internet (same as --quick today)",
    )
    args = ap.parse_args()
    results = asyncio.run(run_checks(quick=args.quick, network=not args.no_network))
    raise SystemExit(_render(results))


if __name__ == "__main__":
    main()
