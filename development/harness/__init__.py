"""disco test harness — record/replay cassettes + runners.

The substrate for the testing surfaces (universal-readiness testing plan): a
VCR-style record/replay layer that captures VERBATIM responses from the real
services (LLM, search, extraction, encoders, sandbox) once, and replays them at
the existing `ConversationRuntime(...)` injection seams so tests exercise the
REAL contract deterministically — never a hand-written approximation.

Not shipped to prod; injected only in tests + the eval/replay/fault runners.
"""
