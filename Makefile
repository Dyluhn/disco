# perpleximanus — local test/verify runners (no CI; this IS the runner).
# Two halves (see ~/.claude/plans/fancy-sauteeing-ocean.md):
#   hermetic  → in-process, replay/fake, fast — `make test`
#   live      → drives the real running app / real services — `make canary`, `make capture`
#
# Targets that need a real model or the heavy research cassette are kept SEPARATE
# from `make test` so the fast path stays fast and offline.

BASE ?= http://localhost:8001          # agent-server base URL for live probes
CASSETTE ?= harness/cassettes/research_demo.jsonl

.PHONY: help test unit harness contract fuzz fault eval eval-real replay \
        canary canary-health capture capture-loop lint fmt e2e verify

help:
	@echo "perpleximanus runners:"
	@echo "  make test        unit + harness (contract/fuzz/fault/canary) — fast, hermetic, offline"
	@echo "  make unit        per-package unit suite only (packages/*/tests)"
	@echo "  make harness     all harness/ tests"
	@echo "  make contract    TS<->Python wire/event contract drift"
	@echo "  make fuzz        property-based parser fuzzing (hypothesis)"
	@echo "  make fault       fault/chaos injection — resilience fires"
	@echo "  make lint        ruff check (all packages + harness)"
	@echo "  make eval        research eval, REPLAY mode (needs $(CASSETTE) — run 'make capture')"
	@echo "  make eval-real   research eval, REAL services (slow; needs live config+secrets)"
	@echo "  make verify      'pmx verify' — does YOUR configured model drive the loop? (ARGS=--quick)"
	@echo "  make capture     record the real research cassette (HEAVY: cold fastembed + LLM)"
	@echo "  make canary      live probe of $(BASE): /health + a real grounded research query"
	@echo "  make canary-health  live /health probe only (no model call)"
	@echo "  make replay      event-log deterministic replay (needs loop_demo fixture — 'make capture-loop')"
	@echo "  make capture-loop  record a real deep-research conversation (event-log + cassette) — HEAVY"
	@echo "  make e2e         frontend Playwright E2E + visual regression (fixture mode)"

# ---- hermetic (fast, offline) ----------------------------------------------

test: unit harness

unit:
	uv run pytest

harness:
	PYTHONPATH=. uv run pytest harness/tests

contract:
	PYTHONPATH=. uv run pytest harness/tests/test_contract.py

fuzz:
	PYTHONPATH=. uv run pytest harness/tests/test_fuzz.py

fault:
	PYTHONPATH=. uv run pytest harness/tests/test_faults.py

lint:
	uv run ruff check packages harness

fmt:
	uv run ruff format packages harness

# ---- evals (replay = fast; real = slow, hits services) ---------------------

eval:
	@test -f $(CASSETTE) || { echo "missing $(CASSETTE) — run 'make capture' first"; exit 1; }
	PYTHONPATH=. uv run python -m harness.eval_runner --replay --cassette $(CASSETTE)

eval-real:
	PYTHONPATH=. uv run python -m harness.eval_runner

# `pmx verify` — the user-facing setup check: does YOUR configured model drive the
# loop? (config + completion + tool-calling + grounding, pass/fail). --quick skips
# the live grounding step. Reads the persisted config/secrets like the servers do.
verify:
	uv run python -m perpleximanus.agent_server.verify $(ARGS)

# Heavy: cold fastembed + a real LLM + the grounding self-correction loop. Detached
# (setsid) so an interactive-session timeout can't SIGKILL it before fastembed loads.
capture:
	PYTHONPATH=. setsid uv run python -m harness._capture_research_demo

# Heavy: a real deep-research conversation through the agent loop → the (event-log +
# cassette) pair the Phase 3 replay reproduces. Detached for the same reason.
capture-loop:
	PYTHONPATH=. setsid uv run python -m harness._capture_loop_demo

# ---- live probes (drive the deployed instance) -----------------------------

canary:
	PYTHONPATH=. uv run python -m harness.canary --base $(BASE)

canary-health:
	PYTHONPATH=. uv run python -m harness.canary --base $(BASE) --no-research

# ---- event-log deterministic replay (Phase 3) ------------------------------

replay:
	PYTHONPATH=. uv run python -m harness.replay_runner

# ---- Phase 7 — frontend E2E + visual regression (fixture mode, no backend) --

e2e:
	cd frontend && npm run test:e2e

e2e-update:
	cd frontend && npm run test:e2e:update
