# Scoped change enforcement bootstrap acceptance

Owner-authorized enforcement work is isolated from product fixes and the prior
unreleased development branch. This change adds a PR-body declaration validator,
a trusted-base diagnostic workflow, a PR template, and contributor guidance.
Manual comprehensive UI acceptance remains outside CI. No LLM protocol, provider
selection, budgets, retries, output criteria, release behavior or dependency
versions change.

Owning transition: `PKG-27-SCOPED-CHANGE-ENFORCEMENT`. The new
`development/tests/architecture/test_change_scope.py` contributes exactly eighteen
static and collected IDs, with no existing IDs removed or renamed. The unchanged
mandatory collectors observed Python files 974, Python static IDs 11853, logical
`tests` root 486 and collected total 14528; every other population, fixture,
marker and selection remained unchanged. Existing inventory assertions advance
only these independently observed counts. Public API changes are expected to be
zero and must be checked by the unchanged official scanner before regeneration.

The scoped tests check missing/ambiguous/untrusted records, exact base/head/tree
binding, scope drift, renamed/deleted paths, provider-evidence declarations and
sensitive-control changes. Execution output and exact commit/tree binding belong
in the external validation receipt and PR body after the final authority amend.
The eighteen cases pass after formatting and extraction of provider/evidence
validation into bounded helpers. The architecture scan reports zero violations
and zero debt; final authority and focused checks remain required before landing. No broader release or
model-quality certification is asserted.

The workflow is absent from the trusted base during bootstrap. Its read-only
`pull_request_target` job is base-context diagnostic evidence and emits no
explicit candidate-head status. Declarations do not authenticate test execution
or approve sensitive changes. A separately held approval/status issuer is still
required for a real independent boundary; shared-account agent review does not
establish it. Existing required CI and repository protections continue to apply.

The official metadata transition preserves all previous authority rows and test
identities. Only public API authority, test inventory authority and their existing
governance seal are regenerated through the unchanged procedures. No exclusions,
allowlists, checker relaxations or historical fixture rewrites are authorized.
