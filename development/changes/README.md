# Scoped changes and trusted evidence

This is a bootstrap proposal, not an activated repository protection. The first
PR adds the checker/workflow, so its base cannot run this new check. Existing CI
and owner review still apply. No product fixes from the accumulated branch are
part of this change.

Every later PR changes one JSON file here. Its version1 record contains exactly:

- `id`, `issue` (issue link or identified owner request), `purpose`, `reproduction`;
- `base`: the current PR base SHA; `paths`: exact changed files, including the record;
- `risks`: nonempty explanations for `context_limits`, `budgets`, `retries`,
  `fallbacks`, `success_criteria`, and `compatibility`;
- `provider`: boolean `applicable`, `reason`, and `variant_tests` identifiers;
- `evidence`: tested `commit`, its Git `tree`, and nonempty `checks` containing
  `command`, integer `exit`, log/artifact `artifact_sha256`, and `tests` IDs;
- `ui` and `quality`: separate `status` and `evidence`. Status is `not_applicable`,
  `untested`, `failed`, or `passed_scoped`. No fabricated universal pass.

Changes to LLM/research source cannot opt out of provider evidence. Other areas
must classify applicability honestly. When applicable, listed variant tests
must occur in execution evidence. Tests should compare arbitrary renamed
models/endpoints with identical explicit capabilities, familiar-name negative
controls, missing metadata/usage, optional-field handling, and streaming
variants relevant to the change. A separate real-provider compatibility audit
is still required; a string in this record does not prove broad compatibility.

Run tests on a committed source revision, retain the actual output, then add or
update only the change record in a final metadata commit. The checker requires
that no other file differs after the tested commit. This avoids an impossible
self-referential commit hash. Any later code/test change requires fresh evidence.
Log hashes bind declarations to artifacts but cannot establish that an unsigned
log was executed; retain the originals for independent review/trusted execution.

Invoke **the trusted base copy**, not a candidate-supplied checker:

```sh
python3 development/scripts/check_change_scope.py --repo . --base BASE_SHA --head HEAD_SHA
```

The validator reads Git objects only. It rejects missing/ambiguous records,
unknown/duplicate fields, scope drift, stale evidence and incomplete applicable
provider declarations. Exit0 means scope/evidence declarations are consistent;
it never certifies quality. Exit1 is invalid. Exit2 requires independent review
for check/workflow/fixture/release/control changes and edits/removals of existing
tests. No `approved` field or candidate-provided exception grants approval.

## Trusted invocation and bootstrap limits

`change-scope.yml` uses `pull_request_target`, pinned checkout, immutable base
SHA and a read-only token. It fetches PR objects from the fixed public repository,
verifies the exact event head, and executes only base code. No candidate checkout,
action, dependency installation, subprocess command, secret or publishing token
is executed in that trusted lane. Candidate files are untrusted Git/JSON data.
A private-repository migration needs a separately reviewed fetch design.

Sensitive changes intentionally remain blocked in this lane. Owner-controlled
review/transition handling must be installed outside candidate authority before
such changes can land through an enforced required check; do not add an in-record
bypass. The initial bootstrap needs explicit owner review and existing checks.
The workflow's presence alone does not protect main or configure a required check.
Its GitHub Actions check identity is shared with other workflows and is not an
unspoofable independent verifier. A separately credentialed GitHub App emitting
head-bound status from pinned trusted policy is needed for that stronger boundary.

Current preflight found main unprotected, one owner/admin identity accessible to
implementation, and no independent reviewer/publication identity. Native rules
should block direct/force/delete pushes, require fresh review of the latest head
and required checks, and dismiss stale approval. Owner admin access remains able
to change those rules. Never simulate independence with another subagent using
the same account. Test deliberate rejections on disposable refs/fixtures first.

Manual comprehensive UI acceptance remains **outside CI**. Its real controls,
workflows, persistence, failures and untested states are release evidence separate
from implementation tests and output-quality review. This workflow does not add
a CI browser sweep or claim a scripted test demonstrates real model quality.

Publishing stays on the workstation CLI, with approved source, build recipe,
artifact digests and public verification. This PR does not change release rules
or grant publication access. Separating that credential from implementation and
replacing the existing alternate Actions publishing path need owner review.
