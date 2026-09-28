## Problem and scope

Describe the reproduced problem and resulting behavior. Link the issue or owner
request and the single `development/changes/*.json` record. List intentional
limits and any required dependency; unrelated discoveries need their own change.

## Validation and risks

Link exact-source test output/artifact hashes. Explain changes to context/source
limits, budgets, retries, fallback and success criteria, including functionality
or quality lost. State provider applicability and relevant arbitrary-name,
capability, missing-metadata/usage and streaming variant evidence.

Keep implementation tests, actual UI workflow checks, and output-quality review
separate. Record failures/untested states. Manual comprehensive UI acceptance is
not a CI requirement; screenshots alone do not prove a workflow or persistence.

## Independent review and delivery

Identify changes to checks, exclusions, existing tests, authority inventories,
replay fixtures or release rules for separate review. Historical fixtures must
remain immutable; never rekey old responses. Candidate records cannot approve
their own control changes. Publication remains through the workstation CLI.

Bootstrap only: explicitly state when the new trusted-base workflow does not yet
exist on main and therefore has not run. Existing checks still apply. Do not call
shared-account subagent review an independent access-control boundary.
