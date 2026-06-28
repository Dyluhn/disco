Re-review the 3 required revisions to PR P5-DELIVERY (see "PLAN REVISION 1" appended to
/home/dylan/projects/disclaude/disclaude.md). You APPROVED the audit + the delivery_mode derived property +
the app|files mapping last round. The 3 fixes:
(1) Reworded scope: P5 ships contract delivery SHAPE + a validator that MAKES shape enforceable + a runtime
accessor — NOT runtime enforcement. Runtime enforcement (reject wrong-shape handoff) is the handle_serve wire
follow-up. No "validator enforces shape" overclaim.
(2) Port-8000: tracked as its own small PR P5-PORT (careful engine.py serve/finish reconcile: platform owns
the sandbox port via preview_start; 8000 is only the canonical user-visible proxy — verify_app.py:42/
server.py:18 — so the canonical-port contract is preserved) with full finish-gate regression. NOT bolted onto
the delivery-shape PR.
(3) Reject-wire follow-up expanded to cover BOTH handle_serve AND the lifecycle synthetic app-deliverable
path (artifact_kind="app" for any index.html regardless of contract); both will read delivery_mode when wired.
Confirm these resolve your 3 required revisions and the scoping (P5-DELIVERY = shape+validator+accessor+tests;
P5-PORT + reject-wire tracked). Return APPROVE or REVISE + one line each.
