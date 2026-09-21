"""Disco Build Soak harness — deterministic evidence oracle for the bare Build loop.

This package is the *foundation* slice of the Build Soak
(development/notes/build-soak-guidelines.md):
the no-live-spend, deterministic core — evidence lock, oracle result schema, the
event normalizer, the deterministic classifier, the core oracles, and the
fake-model/fake-tool simulator scaffolding.

It is intentionally STANDALONE: it parses Disco event logs as plain JSON dicts and
never imports `disco.core` (or any product package). This keeps the adjudicator a
pure function of durable evidence — it cannot be biased by product types — and keeps
the package-layering contract (.importlinter) untouched (the harness is not in the
`disco` namespace and pulls in none of it).

The classifier MUST NOT call an LLM. The oracle adjudicates; agents only repair.
"""
