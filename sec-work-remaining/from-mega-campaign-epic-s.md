# EPIC S — Security Hardening (the closing epic)

> Extracted from `docs/disco-mega-campaign.md` (segregation 2026-07-06). The campaign
> ledger keeps a one-line pointer here and still tracks per-wave DONE/PARKED status in
> its state table. **Current state of record: `disco-security-state.md`** in this folder.

Executes the **already-implementation-ready** `disco-security-fix-campaign.md` (7-round
adversarial Opus+codex convergence; **38 findings = 8 Critical · 17 High · 10 Medium · 3
Low**). **Root cause:** 38 findings are ~5 problems; **ROOT-A (no-auth + wildcard CORS) is
the keystone** — fixing it removes the reachability of ~9 findings. Full per-task detail +
acceptance tests live in that doc; the waves are the WOs:

- **S-W1 — ROOT-A: auth + CORS + owner-scoping (keystone).** HttpOnly SameSite cookie + CSRF + strict WS Origin + scoped preview/artifact capabilities + generated route-inventory test + admin-only global app-server state. Closes C5,H6,H8; removes reachability of H2,H10,H13,H14,M2,M7. Tasks A1–A8 in the security doc. **DONE `e028d2ac`.**
- **S-W2 — ROOT-B: secret-resolution + egress chokepoint.** SecretStore-only provider map (kill the os.environ fallthrough, C8), two egress classes (untrusted hard-deny private vs operator-configured origin-pinned), WeasyPrint asset allowlist, `runs_in` honesty for remote image/audio/slides backends. Closes C4,C8,H1,H11,H12,M3,M5,M7,C7. **DONE `17c47761` (tree-identical to archived `2408e40f`).**
- **S-W3 — host-execution cluster (gVisor bypass).** Fail-closed backend Literal, plan/DoD command predicates run IN-sandbox, hardened deny floor, kernel-token hygiene. Closes C1,C2,C3,C6,H9,M1,H13. **DONE `1b762e3f`.**
- **S-W4 — MCP approval integrity.** Pre-CONNECT approval, first-use approval, stdio host mislabel, risk-tier wiring, inputSchema in the approval hash. Closes H3,H4,H5,H7. **DONE `212f6e89`.**
- **S-W5 — availability / isolation.** Per-surface egress policy + host-enforced private+tailnet block + no sibling hairpin, real workspace quota, sandbox→host transfer caps, DoD no-auto-release, preview argv-not-shell. Closes H15,H16,H17,M8,M9,M10. **PARKED.**
- **S-W6 — output sinks + share/storage + lows.** Share point-in-time snapshot, storage-browse jail, xlsx formula injection, dead redaction, .env untrack, KDF note. Closes M4,M2,M6,L1,L2,L3. **PARKED.**
- **S-post — re-audit:** one more Opus+codex round-pair against the patched tree to confirm the tail collapsed.

Per-wave HARD GATES (from the security doc): build + real-sample harness → **exercise the
real exploit pre-fix (prove it works) then post-fix (prove it's closed)** → gpt-5.5
adversarial to SHIP → commit. Line numbers in the security doc are from 2026-06-20 and WILL
have drifted — **match on content/symbol, not line number** (use Serena).

State summary: **S-W1 / S-W2 / S-W-Pi / S-W3 / S-W4 DONE; S-W5 / S-W6 PARKED** (Dylan's call
— prerequisite for any public/hardened release, not in flight). The resume playbook + grounded
recon live in `disco-security-fix-campaign.md`; the authoritative state is `disco-security-state.md`.
