# Governance — the authority map

This directory is the **finite authority surface** for agents and humans working
in this repository. If a question is about what is required, what is currently
true, or what happens next, the answer is here or it is not authoritative.

## Authority order

When two sources disagree, the higher entry wins.

| # | Source | Authority |
|---|--------|-----------|
| 1 | **Current code + passing tests + live evidence** | Reality. Beats every document. |
| 2 | [`ENGINEERING-STANDARDS.md`](./ENGINEERING-STANDARDS.md) | How work is done. Sealed. |
| 3 | [`ARCHITECTURE-BOUNDARIES.md`](./ARCHITECTURE-BOUNDARIES.md) | Invariants a change may not break. Sealed. |
| 4 | [`CAMPAIGN-PLAN.md`](./CAMPAIGN-PLAN.md) | The finite current campaign and its acceptance contract. Change-controlled. |
| 5 | [`CURRENT-STATE.md`](./CURRENT-STATE.md) / [`CAMPAIGN-STATUS.md`](./CAMPAIGN-STATUS.md) | What is true right now. Mutable. |
| 6 | [`ARCHITECTURE-ROADMAP.md`](./ARCHITECTURE-ROADMAP.md) | What comes after the current campaign. Mutable. |
| 7 | [`RELIABILITY-PATTERNS.md`](./RELIABILITY-PATTERNS.md) / [`SELF-REVIEWS.md`](./SELF-REVIEWS.md) | Learned patterns and the review log. Append-only. |
| 8 | Design/code contracts in the repository root | Durable design authority for their subject. |
| 9 | Everything else | Non-authoritative. |

Root [`CLAUDE.md`](../../CLAUDE.md) is a **bootstrap**: verified navigation,
layering, commands, and durable gotchas. It points here for authority and holds
no status of its own.

## The files

| File | Purpose | Mutability |
|------|---------|------------|
| `README.md` | This authority map. | Mutable (structure rarely changes). |
| `ENGINEERING-STANDARDS.md` | Durable product philosophy, development/architecture/implementation rules, root-cause and testing discipline, autonomy, evidence integrity. | **SEALED** — hash-gated, owner-only rebaseline. |
| `ARCHITECTURE-BOUNDARIES.md` | Stable architectural invariants, reconciled against real code. | **SEALED** — hash-gated, owner-only rebaseline. |
| `ARCHITECTURE-ROADMAP.md` | Post-campaign architectural sequence (Tier A/B/C) and carried-forward research/owner gates. | Mutable. |
| `CURRENT-STATE.md` | Concise, evidence-linked current tree/branch state and honest known limitations. | Mutable. |
| `CAMPAIGN-PLAN.md` | The finite current campaign: epics, acceptance criteria, scope boundary. | Change-controlled once reconciled. |
| `CAMPAIGN-STATUS.md` | The standing operational ledger. Current snapshot on top, append-only log below. | Mutable, updated constantly. |
| `RELIABILITY-PATTERNS.md` | Evidence-backed recurring bug patterns and their structural remedies. | Append-only. |
| `SELF-REVIEWS.md` | Hourly self-reviews. | Append-only. |
| `PROTECTED.sha256` | The seal manifest for the two sealed files. | Owner-only. |

## What is NOT authoritative

The following are **history**. Read them only for a specific code-archaeology
question. They are never status, never policy, and never a source of open work:

- `archive/` and `archive/docs/` in this repository;
- any dated handoff, "current status", session findings, or work-order file
  outside this directory;
- old campaign evidence directories outside this repository;
- the external pre-reset context archive at
  `/var/home/dylan/disclaude-context-archive/2026-07-25-pre-reset/`.

Files removed during the context reset were preserved in that external archive
with a `MANIFEST.sha256`. **They must not be restored into the repository.**

## The seal

`ENGINEERING-STANDARDS.md` and `ARCHITECTURE-BOUNDARIES.md` are change-controlled
by four independent mechanisms:

1. **Tracked manifest** — `PROTECTED.sha256` holds their SHA-256 digests.
2. **Deterministic gate** — `development/scripts/check_governance_seal.py` recomputes and
   compares; non-zero exit on drift. This is the authoritative backstop.
3. **PreToolUse guard** — `.claude/hooks/governance_guard.py` denies `Edit`,
   `Write`, `NotebookEdit`, and obvious `Bash` mutations targeting a sealed file.
4. **SessionStart check** — `.claude/hooks/session_start.py` reports seal
   integrity at session start.

"Immutable" here means *an agent cannot silently drift these standards*: a
mutation is blocked, or it fails the required gate. It does not mean the file is
read-only at the filesystem level — no `chattr`, no root ownership, and no
permission trick that would break Git, Windows, or macOS.

### Owner-only rebaseline procedure

A rebaseline requires an **explicit instruction from the owner**. An agent may
not initiate one.

```bash
# 1. Owner explicitly authorises the change and states what changes and why.
# 2. Edit the sealed file with the guard bypassed for that single command:
DISCO_GOVERNANCE_REBASELINE=1 $EDITOR current/docs/governance/ENGINEERING-STANDARDS.md
# 3. Regenerate the manifest:
DISCO_GOVERNANCE_REBASELINE=1 .venv/bin/python3 development/scripts/check_governance_seal.py --rebaseline
# 4. Verify the gate passes and commit the doc + manifest together,
#    recording the owner authorisation in the commit message.
.venv/bin/python3 development/scripts/check_governance_seal.py
```

The bypass variable exists so the owner can perform a deliberate, auditable
change. Setting it without an explicit owner instruction is a governance
violation, not a shortcut.
