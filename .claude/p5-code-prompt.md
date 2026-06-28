# Codex CODE Review — P5-DELIVERY (contract delivery shape + validator + accessor) — IMPLEMENTED

You APPROVED this plan (P5-DELIVERY = shape+validator+accessor+tests; P5-PORT + reject-wire tracked separate).

Inspect:
- core/contract/models.py: DeliveryMode = Literal["app","files"]; delivery_mode_for_kind(kind) (app for
  appkit.leadgen/static.site/interactive.prototype; files otherwise incl. custom); ArtifactContract.delivery_
  mode @property (derived from kind, can't drift); deliverable_kind_matches_contract(contract, artifact_kind)
  validator (artifact_kind == contract.artifact.delivery_mode). Exported from contract/__init__.
- agent-server/runtime.py: expected_delivery_mode(conversation_id) → "app"|"files"|None. Resolves the
  conversation's contract (only for artifact-mode OR declared-kind runs; None for a plain chat — does NOT
  fabricate a contract); reads artifact.delivery_mode.
- tests core/test_contract_delivery.py (6: per-kind mapping, custom→files, property↔kind for every builtin,
  validator accept + reject wrong-shape, app-open vs files-download) + agent-server accessor test
  (plain→None, appkit→app, deck→files).

Tests: 32 green (delivery + activation + contract models/registry regression). basedpyright: contract tree 0
errors; runtime 1 error = pre-existing ScheduleService.fire_now (unrelated, stash-confirmed earlier).

This PR is SHAPE + VALIDATOR + ACCESSOR only (no runtime enforcement — handle_serve/synthetic-deliverable
reject-wire + the port-8000 reconcile are TRACKED follow-ups, per your approved scoping).

Judge: (a) is the derived delivery_mode property + the kind mapping correct + coherent with the existing
DeliverableEvent app|files axis? (b) is the validator correct (a deck can't hand off as app, an appkit not as
files)? (c) is the runtime accessor sound — only resolving for build/artifact runs, None otherwise, no
contract fabrication for plain chats? (d) does the PR correctly STOP at shape+validator+accessor (no
overclaim of enforcement)? (e) test sufficiency. Return APPROVE|REVISE|BLOCKED_CODEX_UNAVAILABLE + REASONS +
REQUIRED_REVISIONS.
