"""Epic F3.1 — the `form` primitive through the REAL `app_add_primitive` tool.

Exercises the whole tool-layer cycle on an in-memory sandbox: app_create a real
lead_gen app, add a form via app_add_primitive (spec validate → apply_spec fold →
full-tree regenerate → design_lint gate → write-on-diff), then the refusal
paths: invalid spec (schema carried), unknown page (known ids listed), identical
re-apply (loud refusal, never a hollow success), wrong-host (directory), and the
form-as-base-scaffold misuse of app_create.
"""

from __future__ import annotations

import json

from disco.core.appkit import APPSPEC_RELPATH
from disco.tools.anatomy import Capability, ToolContext, ToolOutcome
from disco.tools.builtin.app_kit import (
    AppAddPrimitiveArgs,
    AppAddPrimitiveTool,
    AppCreateArgs,
    AppCreateTool,
)
from tool_fakes import FakeSandboxInstance

_FORM_SPEC: dict[str, object] = {
    "form_id": "quote_request",
    "title": "Request a quote",
    "fields": [
        {"name": "full_name", "label": "Full name", "kind": "text", "required": True},
        {"name": "email", "label": "Email", "kind": "email", "required": True},
        {"name": "details", "label": "Project details", "kind": "textarea"},
        {"name": "budget", "label": "Budget (USD)", "kind": "number"},
        {"name": "subscribe", "label": "Subscribe", "kind": "checkbox"},
    ],
    "success_message": "Got it — we'll send a quote within two business days.",
}


def _ctx(sbx: FakeSandboxInstance) -> ToolContext:
    return ToolContext(
        sandbox=sbx,
        workspace_path=".",
        timeout_s=30,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id="conv-f31",
    )


async def _create_app(sbx: FakeSandboxInstance, primitive_id: str) -> ToolOutcome:
    return await AppCreateTool().run(
        AppCreateArgs(
            recipe_id="editorial-ledger", primitive_id=primitive_id, brief="Acme Studio"
        ),
        _ctx(sbx),
    )


async def _add_form(sbx: FakeSandboxInstance, spec: dict[str, object]) -> ToolOutcome:
    return await AppAddPrimitiveTool().run(
        AppAddPrimitiveArgs(primitive_id="form", spec=dict(spec)), _ctx(sbx)
    )


# ---- the happy path ----------------------------------------------------------------


async def test_add_form_happy_path_regenerates_tree_and_persists_record():
    sbx = FakeSandboxInstance()
    created = await _create_app(sbx, "lead_gen")
    assert created.success is True, created.content

    out = await _add_form(sbx, _FORM_SPEC)
    assert out.success is True, out.content
    assert out.structured is not None
    assert out.structured["tier"] == "fillable"
    touched = out.structured["files_written"]
    assert "schema.sql" in touched
    assert "worker/index.ts" in touched
    assert "src/db/schema.ts" in touched
    assert "src/App.tsx" in touched
    assert any("QuoteRequest" in path for path in touched)

    # the regenerated tree carries the whole form plane
    schema = sbx._fs["schema.sql"].decode("utf-8")
    assert 'CREATE TABLE IF NOT EXISTS "quote_requests"' in schema
    worker = sbx._fs["worker/index.ts"].decode("utf-8")
    assert '"/api/quote_requests"' in worker
    assert "return json({ error: check.error }, 422);" in worker
    assert "missing required field" in worker
    # the lead plane survives untouched
    assert 'url.pathname === "/api/leads" && request.method === "POST"' in worker
    comp = sbx._fs["src/components/HomeQuoteRequestSection.tsx"].decode("utf-8")
    for name in ("full_name", "email", "details", "budget", "subscribe"):
        assert f'name="{name}"' in comp
    assert "Got it — we'll send a quote within two business days." in comp

    # the AppSpec was re-saved with the folded entity/section/action
    app_data = json.loads(sbx._fs[APPSPEC_RELPATH])
    assert any(e["id"] == "quote_request" for e in app_data["entities"])
    home_sections = app_data["pages"][0]["sections"]
    form_section = next(s for s in home_sections if s["id"] == "quote_request")
    assert form_section["kind"] == "form"
    assert form_section["content_ref"] == "quote_request"
    assert home_sections[-1]["kind"] == "footer"  # footer stays last
    assert any(a["id"] == "submit_quote_request" for a in app_data["primary_actions"])

    # provenance record persisted with the validated spec
    record = json.loads(sbx._fs[".disco/primitives/form.json"])
    assert record["primitive_id"] == "form"
    assert record["tier"] == "fillable"
    assert record["spec"]["form_id"] == "quote_request"
    assert len(record["spec"]["fields"]) == 5


# ---- refusals ------------------------------------------------------------------------


async def test_identical_reapply_is_refused_loudly():
    sbx = FakeSandboxInstance()
    assert (await _create_app(sbx, "lead_gen")).success is True
    first = await _add_form(sbx, _FORM_SPEC)
    assert first.success is True, first.content
    worker_after_first = sbx._fs["worker/index.ts"]

    again = await _add_form(sbx, _FORM_SPEC)
    assert again.success is False
    assert "already exists" in again.content
    # nothing changed on the second attempt
    assert sbx._fs["worker/index.ts"] == worker_after_first


async def test_invalid_spec_refused_with_expected_schema():
    sbx = FakeSandboxInstance()
    assert (await _create_app(sbx, "lead_gen")).success is True
    out = await _add_form(sbx, {**_FORM_SPEC, "captcha": True})
    assert out.success is False
    assert "invalid 'form' spec" in out.content
    assert "Extra inputs are not permitted" in out.content
    # the expected schema is carried in the refusal (self-recovering); it is
    # truncated to 600 chars, so assert its presence rather than a deep key
    assert "Expected schema" in out.content
    assert "FormField" in out.content


async def test_unknown_page_refused_with_known_page_ids():
    sbx = FakeSandboxInstance()
    assert (await _create_app(sbx, "lead_gen")).success is True
    out = await _add_form(sbx, {**_FORM_SPEC, "page_id": "pricing"})
    assert out.success is False
    assert "unknown page_id" in out.content
    assert "home" in out.content  # the known-page list is carried


async def test_directory_host_refused_with_guidance():
    sbx = FakeSandboxInstance()
    assert (await _create_app(sbx, "directory")).success is True
    out = await _add_form(sbx, _FORM_SPEC)
    assert out.success is False
    assert "lead_gen-shaped" in out.content


async def test_app_create_with_form_primitive_fails_with_addon_guidance():
    sbx = FakeSandboxInstance()
    try:
        out = await _create_app(sbx, "form")
    except ValueError as exc:
        # the executor maps a raised tool error to a failed result; calling the
        # tool directly surfaces the same actionable message
        assert "ADD-ON" in str(exc)
    else:
        assert out.success is False
        assert "ADD-ON" in out.content
