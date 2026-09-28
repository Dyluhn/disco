# Scoped PR changes and trusted evidence

This is a bootstrap proposal, not activated repository protection. The first PR
adds the checker/workflow, so its base cannot run this new check. Existing CI and
owner review still apply. No product fixes from the accumulated branch are part
of this change.

Put exactly one JSON block labeled `disco-change` in the PR body. It is untrusted
data, never a command. Version1 requires exactly these fields:

- `id`, `issue` (issue link or identified owner request), `purpose`, `reproduction`;
- `base`: current PR base SHA; `paths`: every exact changed file, including all
  source, tests, documentation and generated authority metadata;
- `risks`: explanations for `context_limits`, `budgets`, `retries`, `fallbacks`,
  `success_criteria`, and `compatibility`;
- `provider`: boolean `applicable`, `reason`, and `variant_tests` identifiers;
- `evidence`: exact tested head `commit`, its Git `tree`, and nonempty `checks`
  containing `command`, integer `exit`, log/artifact `artifact_sha256`, and `tests` IDs;
- `ui` and `quality`: separate `status` and `evidence`. Status is `not_applicable`,
  `untested`, `failed`, or `passed_scoped`. No fabricated universal pass.

Changes to LLM/research source cannot opt out of provider evidence. Other areas
must classify applicability honestly. Applicable variant tests must appear in
execution evidence. They should compare arbitrary renamed models/endpoints with
identical explicit capabilities, familiar-name negative controls, missing
metadata/usage, optional-field handling and relevant streaming variants. A
separate real-provider compatibility audit remains necessary; a string in this
record does not prove broad compatibility.

Finish the source and official derived-authority commit, run checks, retain the
original output, then edit the PR body with that exact head/tree and evidence.
Editing the body does not change the Git commit; its `edited` event reruns the
scope check. A tested parent is rejected even for metadata-only commits. There
are no excluded files or circular self-hash conventions. Hashes bind declarations
to artifacts but do not prove that an unsigned log was executed; independent
checks/review must establish authenticity. A consistent record is not approval.

Invoke **the trusted base copy**, never candidate-supplied code:

```sh
python3 development/scripts/check_change_scope.py --repo . --base BASE_SHA --head HEAD_SHA --event /path/to/github-event.json
```

The validator reads `pull_request.body` from runner event JSON and Git objects.
It rejects missing/ambiguous/oversized records, unknown/duplicate fields, scope
drift, stale evidence and missing applicable variant evidence. Exit0 means
scope/evidence declarations are consistent, not quality. Exit1 is invalid.
Exit2 requires independent review for check/workflow/replay/fixture/release/control
changes and edits/removals of existing tests. No candidate `approved` field or
exception can authorize those changes.

## Trusted invocation and bootstrap limits

`change-scope.yml` uses `pull_request_target`, pinned checkout, immutable base SHA
and a read-only token. It fetches PR objects from the fixed public repository,
verifies the event head and executes only base code. PR text comes from
`GITHUB_EVENT_PATH`, never shell/source interpolation. No candidate checkout,
action, dependencies, scripts, publishing tokens or browser sweep run in this
trusted lane. A private-repository migration needs a reviewed fetch design.

Sensitive changes intentionally remain blocked here. Owner-controlled independent
review/transition handling must exist outside candidate authority before this can
be activated as a required gate for those changes; do not add in-record bypasses.
The initial bootstrap uses explicit owner review and existing checks. Merely
merging a workflow does not protect main or configure a required check. Its
GitHub Actions identity is shared with other workflows, not an unspoofable
independent verifier. Stronger enforcement needs a separately held GitHub App
identity issuing head-bound status from pinned trusted policy.

Preflight found main unprotected, one owner/admin identity accessible to
implementation and no independent reviewer/publication identity. Native rules
should prohibit direct/force/delete pushes, require checks and fresh approval of
the latest head, and dismiss stale approval. Owner admin power to change those
rules remains. A subagent on the same account is not an independent boundary.
Test rejection cases on disposable refs/fixtures before enabling restrictions.

Manual comprehensive UI acceptance stays **outside CI**. Real controls, workflow
results, persistence, failures and untested states are release evidence separate
from implementation tests and output-quality assessment. This check adds no CI
browser sweep and no claim that scripted tests establish real model quality.

The owner moved all Disco work to blackbox on 2026-09-28; the earlier workstation
publishing instruction is historical and superseded. Publication still needs
approved source, build recipe, digests and public verification. This PR does not
change release rules or grant publication authority. Credential separation and
review of alternate publishing paths remain owner-controlled followups.
