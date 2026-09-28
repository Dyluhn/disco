## Problem and scope

Describe the reproduced problem and resulting behavior. Fill exactly one JSON
block below; placeholders fail validation. Link an issue or identified owner
request. Unrelated discoveries need their own change.

```disco-change
{
  "version": 1,
  "id": "",
  "issue": "",
  "purpose": "",
  "reproduction": "",
  "base": "<full PR base SHA>",
  "paths": ["<every exact changed file>"],
  "risks": {
    "context_limits": "",
    "budgets": "",
    "retries": "",
    "fallbacks": "",
    "success_criteria": "",
    "compatibility": ""
  },
  "provider": {"applicable": false, "reason": "", "variant_tests": []},
  "evidence": {
    "commit": "<exact tested PR head SHA>",
    "tree": "<that commit's Git tree SHA>",
    "checks": [{"command": "", "exit": 0, "artifact_sha256": "<actual log hash>", "tests": []}]
  },
  "ui": {"status": "untested", "evidence": ""},
  "quality": {"status": "untested", "evidence": ""}
}
```

## Evidence and independent review

Retain exact-source execution output and artifact links. Finalize source and
metadata before testing; then update this body without a self-referential commit.
Explain functionality/quality lost through limits, budgets, retries, fallbacks or
success criteria. Provide relevant identifier/capability variant evidence.

Keep implementation tests, actual UI workflows and output-quality assessment
separate. Record failures and untested states. Manual comprehensive UI acceptance
is outside CI; a screenshot alone does not establish persistence or a workflow.

Flag changed checks, exclusions, existing tests, replay matching/fixtures,
authority metadata and release rules for independent review. Never rekey old
responses. This record cannot approve its own control changes.

Bootstrap: explicitly say when the trusted-base workflow is not yet on main and
has not run. Existing checks still apply. Shared-account subagent review is not
an independent access-control boundary. Publication needs independent approval
and provenance tied to the reviewed source and artifact digests.
