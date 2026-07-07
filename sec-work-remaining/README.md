# sec-work-remaining/

All security-work planning, segregated out of the main project docs (2026-07-06) so
the feature/packaging plans read clean. This folder is the single home for **security
work — done, parked, and deferred**. Nothing here is on the active feature path; it is
picked up as a deliberate, separate effort.

**State of record → [`disco-security-state.md`](./disco-security-state.md)** (what is
DONE / PARKED / DEFERRED + the fail-closed `template_only` finish gate). Start there.

## Contents

| File | What it is |
|------|-----------|
| [`disco-security-state.md`](./disco-security-state.md) | **Authoritative** security status: waves done/parked/deferred, the fail-closed gate. |
| [`disco-security-fix-campaign.md`](./disco-security-fix-campaign.md) | The implementation-ready fix campaign — 38 findings (8C/17H/10M/3L), per-wave tasks + acceptance tests + resume recon. |
| [`SECURITY.md`](./SECURITY.md) | The repo's public security policy / disclosure doc (was at repo root). |
| [`wo-a2-host-bus-design-notes.md`](./wo-a2-host-bus-design-notes.md) | Host-service bus auth design notes — the per-app bearer deferred from WO-A2.1. |
| [`from-mega-campaign-epic-s.md`](./from-mega-campaign-epic-s.md) | EPIC-S wave breakdown (S-W1..S-W6 + S-post), lifted from `docs/disco-mega-campaign.md`. |
| [`from-builder-primitives-plan-sec7.md`](./from-builder-primitives-plan-sec7.md) | The template-only adversarial-harness table + Fable-forbidden labor routing, lifted from `docs/disco-builder-primitives-plan.md` §7 / §10.4–10.5. |

## Current security state (one-liner)

**S-W1 / S-W2 / S-W-Pi / S-W3 DONE; S-W4 (MCP approval) / S-W5 (isolation+caps) /
S-W6 (output sinks+share) PARKED** at Dylan's call — a prerequisite for any public /
hardened release, not in flight. Plus two fail-closed builder seams whose security fills
are documented **on their own unmerged branches** (not in this folder):
`disclaude/f41-stripe-seam` (`60436fa8`) and `disclaude/f33-webhook-seam` (`ec622888`).

## What deliberately did NOT move (and why)

The ask was to move *sections about security* out of the plans. Three kinds of content
mention security but are **not** "sec work remaining," so they stayed in place:

- **Live-system design contracts** — `basis-of-design.md §17`, `security-analyzer-contract.md`,
  `tool-sandbox-contract.md`, `event-state-contract.md`, and `api-endpoints.md`'s "Auth (S-W1)"
  section describe the **shipped, running** auth / kill-switch / `SecurityRisk` / egress-approval
  system. They are the integration + design authority for code that exists; moving them would make
  those docs lie about the live surface.
- **Records, not plans** — `CHANGELOG.md`'s `### Security` entry (a Keep-a-Changelog log of fixes
  that *already shipped*) and `docs/mega-campaign-run-log.md` (timestamped history, already
  corrected in place). Their cross-references were repointed here, but the history stays put.
- **Interwoven catalog rows** — the `template_only` primitives in `disco-builder-primitives-plan.md`
  §4 (RLS, payment webhooks, uploads, …) are feature-catalog entries tagged by labor class; their
  **adversarial-harness contracts** were lifted here (`from-builder-primitives-plan-sec7.md`), but
  the catalog rows themselves stay so the feature plan remains whole.

If the intent is to pull *those* out too, that's a follow-up — say so and it's a quick pass.
