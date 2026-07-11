"""Epic F3.3 — the `webhook` primitive through the real `app_add_primitive` tool.

Covers, against an in-memory sandbox:
  * happy path on a standalone webhook app — the validated spec folds into the
    AppSpec, the tree regenerates through the app's OWN base primitive, the
    declared endpoint renders in the "Handler pending secure setup" state, and
    the whole regenerated tree stays inert (no worker route / no sig-verify code);
  * provenance — `.disco/primitives/webhook.json` records tier=template_only +
    the complete applied-spec list, and the tool's success carries the Disco-owned note;
  * unsupported foreign bases are refused instead of receiving a false live surface;
  * refusals — invalid spec (unknown key / bad direction) refused WITH the
    expected schema; identical re-apply refused loudly (duplicate endpoint_id —
    never a hollow success).
"""

from __future__ import annotations

import json

from disco.core.appkit import APPSPEC_RELPATH
from disco.core.appkit.webhook_primitive import WEBHOOK_PENDING_MARKER
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin.app_kit import (
    AppAddPrimitiveArgs,
    AppAddPrimitiveTool,
    AppCreateArgs,
    AppCreateTool,
)
from tool_fakes import FakeSandboxInstance

_SPEC = {
    "endpoint_id": "order_events",
    "direction": "inbound",
    "event_types": ["order.created", "order.cancelled"],
    "description": "Order lifecycle receiver.",
}


def _ctx(sbx: FakeSandboxInstance) -> ToolContext:
    return ToolContext(
        sandbox=sbx,
        workspace_path=".",
        timeout_s=30,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id="conv-f33",
    )


async def _create_app(sbx: FakeSandboxInstance, primitive_id: str):
    return await AppCreateTool().run(
        AppCreateArgs(
            recipe_id="editorial-ledger", primitive_id=primitive_id, brief="Acme Studio"
        ),
        _ctx(sbx),
    )


async def _add(sbx: FakeSandboxInstance, spec: dict):
    return await AppAddPrimitiveTool().run(
        AppAddPrimitiveArgs(primitive_id="webhook", spec=spec), _ctx(sbx)
    )


# ---- happy path (webhook base app: fold is VISIBLE in the regenerated tree) -------


async def test_add_webhook_renders_pending_endpoint_and_stays_inert():
    sbx = FakeSandboxInstance()
    created = await _create_app(sbx, "webhook")
    assert created.success is True, created.content
    before = sbx._fs["index.html"].decode("utf-8")
    assert "No webhook endpoints declared yet." in before

    out = await _add(sbx, dict(_SPEC))
    assert out.success is True, out.content
    assert out.structured is not None
    assert out.structured["tier"] == "template_only"
    assert "Disco-owned" in out.content
    assert "do not hand-edit" in out.content

    # the declared endpoint is VISIBLE, in the pending state
    after = sbx._fs["index.html"].decode("utf-8")
    assert WEBHOOK_PENDING_MARKER in after
    assert "order_events" in after
    assert "order.created" in after
    assert "declared, not active" in after

    # …and the regenerated tree is still inert: no worker route, no sig-verify code
    emitted = {
        path: data
        for path, data in sbx._fs.items()
        if not path.startswith(".disco/")
    }
    assert set(emitted) == {"index.html"}
    haystack = after.lower()
    for token in ("hmac", "timingsafeequal", "crypto.subtle", "idempotency",
                  "addeventlistener", "wrangler", "fetch(", "<script", "<form"):
        assert token not in haystack, f"forbidden token {token!r} in index.html"


async def test_provenance_and_appspec_fold_persisted():
    sbx = FakeSandboxInstance()
    assert (await _create_app(sbx, "webhook")).success is True
    assert (await _add(sbx, dict(_SPEC))).success is True

    record = json.loads(sbx._fs[".disco/primitives/webhook.json"])
    assert record["primitive_id"] == "webhook"
    assert record["tier"] == "template_only"
    assert record["specs"] == [_SPEC]

    app_data = json.loads(sbx._fs[APPSPEC_RELPATH])
    page = next(p for p in app_data["pages"] if p["id"] == "webhooks")
    section = next(s for s in page["sections"] if s["id"] == "webhook_order_events")
    assert section["kind"] == "custom"
    assert section["content"]["items"] == ["order.created", "order.cancelled"]
    assert WEBHOOK_PENDING_MARKER in section["content"]["subheading"]


async def test_repeat_add_preserves_complete_endpoint_provenance():
    sbx = FakeSandboxInstance()
    assert (await _create_app(sbx, "webhook")).success is True
    assert (await _add(sbx, dict(_SPEC))).success is True
    second = {
        "endpoint_id": "billing_events",
        "direction": "outbound",
        "event_types": ["invoice.paid"],
        "description": "Billing lifecycle emitter.",
    }
    assert (await _add(sbx, second)).success is True
    record = json.loads(sbx._fs[".disco/primitives/webhook.json"])
    assert record["specs"] == [_SPEC, second]


# ---- unsupported base refusal ------------------------------------------------------


async def test_add_webhook_to_hello_app_is_refused():
    sbx = FakeSandboxInstance()
    assert (await _create_app(sbx, "hello")).success is True
    out = await _add(sbx, dict(_SPEC))
    assert out.success is False
    assert "D1-backed records primitive" in out.content
    assert ".disco/primitives/webhook.json" not in sbx._fs


# ---- refusals -----------------------------------------------------------------------


async def test_invalid_spec_refused_with_schema():
    sbx = FakeSandboxInstance()
    assert (await _create_app(sbx, "webhook")).success is True
    # unknown key (extra=forbid) — the refusal carries the expected schema
    out = await _add(sbx, {**_SPEC, "signing_secret": "shh"})
    assert out.success is False
    assert "invalid 'webhook' spec" in out.content
    assert "Expected schema" in out.content
    assert "endpoint_id" in out.content
    # bad direction literal
    out2 = await _add(sbx, {**_SPEC, "direction": "sideways"})
    assert out2.success is False
    # nothing was applied
    assert ".disco/primitives/webhook.json" not in sbx._fs


async def test_identical_reapply_refused_loudly():
    sbx = FakeSandboxInstance()
    assert (await _create_app(sbx, "webhook")).success is True
    first = await _add(sbx, dict(_SPEC))
    assert first.success is True, first.content
    again = await _add(sbx, dict(_SPEC))
    assert again.success is False
    assert "duplicate webhook endpoint_id" in again.content
    assert again.error == "app_add_primitive_refused"
