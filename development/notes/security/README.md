# current/sec-work-remaining/

All security-work planning, segregated out of the main project docs (2026-07-06) so
the feature/packaging plans read clean. This folder is the single home for **security
work — done, parked, and deferred**. Nothing here is on the active feature path; it is
picked up as a deliberate, separate effort.

**State of record → [`disco-security-state.md`](./disco-security-state.md)** (what is
DONE / DEFERRED + the fail-closed `template_only` finish gate). Start there.

## Contents

| File | What it is |
|------|-----------|
| [`disco-security-state.md`](./disco-security-state.md) | **Authoritative** security status: waves done, separate deferred work, and the fail-closed gate. |
| [`disco-security-fix-campaign.md`](./disco-security-fix-campaign.md) | Completed 38-finding campaign (8C/17H/10M/3L): per-wave implementation, acceptance tests, exploit proof, and adversarial close-out. |
| [`SECURITY.md`](./SECURITY.md) | The repo's public security policy / disclosure doc (was at repo root). |
| [`wo-a2-host-bus-design-notes.md`](./wo-a2-host-bus-design-notes.md) | Host-service bus auth design notes — the per-app bearer deferred from WO-A2.1. |
| [`from-mega-campaign-epic-s.md`](./from-mega-campaign-epic-s.md) | EPIC-S wave breakdown (S-W1..S-W6 + S-post), lifted from `current/docs/disco-mega-campaign.md`. |
| [`from-builder-primitives-plan-sec7.md`](./from-builder-primitives-plan-sec7.md) | Historical template-only harness and labor-routing notes from the superseded builder-primitives campaign. |

## Current security state (one-liner)

**S-W1 / S-W2 / S-W-Pi / S-W3 / S-W4 / S-W5 / S-W6 DONE — and so are the two
builder seams.** The F4.1 Stripe and F3.3 webhook security fills merged long ago
as `8f356c1e` and `ce349096` and are live on `main`: the primitives
(`current/packages/core/src/disco/core/appkit/stripe_primitive.py`,
`webhook_primitive.py`) register real deterministic verifiers plus a mandatory
`live_verify_id` host exploit runner (no longer `verify=None`), backed by host
services in core, live verifiers in agent-server, and config routes in
app-server. All 14 seam test suites pass at `main` (185 passed, 1 skipped;
verified 2026-08-19). Historical provenance only: the former branches
`disclaude/f41-stripe-seam` (`60436fa8`) and `disclaude/f33-webhook-seam`
(`ec622888`) were pruned in the 2026-08-17 branch archiving, and their archived
tips `refs/archive/disclaude/f41-stripe-seam` (`11d3d70f`) /
`refs/archive/disclaude/f33-webhook-seam` (`375e0f14`) are **ancestors of
`main`** — nothing on them is unmerged. The one genuinely outstanding item is
the independent second-model half of the final two-model assurance pass.

## What deliberately did NOT move (and why)

The ask was to move *sections about security* out of the plans. Three kinds of content
mention security but are **not** "sec work remaining," so they stayed in place:

- **Live-system design contracts** — `basis-of-design.md §17`, `security-analyzer-contract.md`,
  `tool-sandbox-contract.md`, `event-state-contract.md`, and `api-endpoints.md`'s "Auth (S-W1)"
  section describe the **shipped, running** auth / kill-switch / `SecurityRisk` / egress-approval
  system. They are the integration + design authority for code that exists; moving them would make
  those docs lie about the live surface.
- **Records, not plans** — `CHANGELOG.md`'s `### Security` entry is the repository history of
  fixes that shipped. Superseded campaign narratives were moved to the external context archive.
- **Historical catalog rows** — the superseded builder-primitives plan was moved to the external
  context archive. Its still-relevant template-only harness notes remain here in
  `from-builder-primitives-plan-sec7.md`.

Current work must use the governance status and campaign files, not these historical records.
