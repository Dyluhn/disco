## What & why

<!-- What does this change, and why? Link an issue if there is one. -->

## Checklist

- [ ] The four architecture fitness gates pass for the area touched: size budget, import
      layering, diagram freshness, and `basedpyright` (zero errors tree-wide). Commands are
      in [CONTRIBUTING.md](../CONTRIBUTING.md#linting--formatting) and `CLAUDE.md`'s
      "Running things".
- [ ] `make test` (or the relevant unit suites) passes.
- [ ] Touches the TS <-> Python wire/event contract? Ran `make contract`.
- [ ] Touches a user-visible surface? Attached a screenshot of it working in the real app
      (see CONTRIBUTING.md's "Verification discipline" — a green test count alone isn't
      evidence for UI work).
- [ ] Commit message(s) follow `type(scope): summary` (CONTRIBUTING.md's "Commit & PR
      conventions").

## Design authority

<!-- If this reinterprets basis-of-design.md or event-state-contract.md, say so and cite the section. -->
