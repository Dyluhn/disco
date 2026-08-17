"""WO-A1 — `app_add_primitive`: the spec fill-and-validate tool.

Covers, through the real tool interface against an in-memory sandbox:
  * an unknown primitive_id is refused (with the known-primitives list);
  * a base scaffold (lead_gen — no spec_schema/apply_spec) is refused with
    app_create guidance;
  * with no app in the workspace the tool refuses and says to app_create first;
  * the happy path on a hello app: the validated HelloSpec headline folds into the
    AppSpec (first page title), the tree regenerates (subtitle visible in
    index.html), and the applied spec is persisted under .disco/primitives/;
  * an invalid spec (unknown key / empty headline) is refused with the expected
    schema carried in the refusal (self-recovering);
  * an identical re-apply is a loud no-op refusal (RC-M), never a hollow success;
  * a template_only primitive's success message carries the Disco-owned /
    spec-only note.
"""

from __future__ import annotations

import json

from disco.core.appkit import APPSPEC_RELPATH
from disco.core.appkit.hello_primitive import HelloSpec, apply_hello_spec
from disco.core.appkit.primitives import PrimitiveDefinition, register_primitive
from disco.tools.anatomy import Capability, ToolContext
from disco.tools.builtin.app_kit import (
    AppAddPrimitiveArgs,
    AppAddPrimitiveTool,
    AppCreateArgs,
    AppCreateTool,
)
from tool_fakes import FakeSandboxInstance

# A test-only template_only primitive (unique id — registered once per process).
# It reuses hello's spec/apply pair; its own generate is never invoked by
# app_add_primitive (the APP's base primitive regenerates the tree).
_TPL_TEST_ID = "wo_a1_template_only_proof"
register_primitive(
    PrimitiveDefinition(
        id=_TPL_TEST_ID,
        default_app_spec=lambda name, recipe: (_ for _ in ()).throw(NotImplementedError),
        prepare_app_spec=lambda app: app,
        generate=lambda app, design: {},
        tier="template_only",
        spec_schema=HelloSpec,
        apply_spec=apply_hello_spec,
    )
)


def _ctx(sbx: FakeSandboxInstance) -> ToolContext:
    return ToolContext(
        sandbox=sbx,
        workspace_path=".",
        timeout_s=30,
        capabilities={Capability.FILESYSTEM},
        owner_id="local",
        conversation_id="conv-wo-a1",
    )


class _FailAppSpecSandbox(FakeSandboxInstance):
    def __init__(self) -> None:
        super().__init__()
        self.fail_appspec = False
        self.commits: list[str] = []

    async def atomic_write(self, path: str, data: bytes) -> None:
        self.commits.append(path)
        if self.fail_appspec and path == APPSPEC_RELPATH:
            raise OSError("injected AppSpec commit failure")
        self._fs[path] = data


async def _create_hello_app(sbx: FakeSandboxInstance):
    return await AppCreateTool().run(
        AppCreateArgs(recipe_id="editorial-ledger", primitive_id="hello", brief="Acme Studio"),
        _ctx(sbx),
    )


async def _add(sbx: FakeSandboxInstance, primitive_id: str, spec: dict):
    return await AppAddPrimitiveTool().run(
        AppAddPrimitiveArgs(primitive_id=primitive_id, spec=spec), _ctx(sbx)
    )


# ---- refusals -------------------------------------------------------------------


async def test_unknown_primitive_refused():
    sbx = FakeSandboxInstance()
    out = await _add(sbx, "no_such_primitive", {"headline": "x"})
    assert out.success is False
    assert "unknown primitive_id" in out.content
    assert "hello" in out.content  # the known-primitives list is carried


async def test_base_scaffold_refused_with_app_create_guidance():
    sbx = FakeSandboxInstance()
    out = await _add(sbx, "lead_gen", {"headline": "x"})
    assert out.success is False
    assert "not spec-addable" in out.content
    assert "app_create" in out.content
    assert "hello" in out.content  # the addable list names hello


async def test_no_app_yet_refused():
    sbx = FakeSandboxInstance()  # empty workspace — no .disco/appspec.json
    out = await _add(sbx, "hello", {"headline": "Welcome"})
    assert out.success is False
    assert "app_create" in out.content


async def test_invalid_spec_refused_with_schema():
    sbx = FakeSandboxInstance()
    assert (await _create_hello_app(sbx)).success is True
    # unknown key (extra=forbid) — the refusal must carry the expected schema
    out = await _add(sbx, "hello", {"headine": "typo-key"})
    assert out.success is False
    assert "invalid 'hello' spec" in out.content
    assert "Expected schema" in out.content
    assert "headline" in out.content
    # bounded: empty headline
    out2 = await _add(sbx, "hello", {"headline": ""})
    assert out2.success is False


# ---- the happy path --------------------------------------------------------------


async def test_apply_hello_spec_updates_tree_and_persists_record():
    sbx = FakeSandboxInstance()
    created = await _create_hello_app(sbx)
    assert created.success is True, created.content
    before = sbx._fs["index.html"].decode("utf-8")
    assert "<p>Home</p>" in before  # the default page title renders as subtitle

    out = await _add(sbx, "hello", {"headline": "Handcrafted since 1994"})
    assert out.success is True, out.content
    assert out.structured is not None
    assert out.structured["tier"] == "fillable"
    # spec-only note is for template_only primitives, not fillable ones
    assert "Disco-owned" not in out.content

    # the folded spec is VISIBLE in the regenerated tree
    after = sbx._fs["index.html"].decode("utf-8")
    assert "<p>Handcrafted since 1994</p>" in after
    assert "<p>Home</p>" not in after

    # the AppSpec was re-saved with the folded title
    app_data = json.loads(sbx._fs[APPSPEC_RELPATH])
    assert app_data["pages"][0]["title"] == "Handcrafted since 1994"

    # provenance record persisted
    record = json.loads(sbx._fs[".disco/primitives/hello.json"])
    assert record["primitive_id"] == "hello"
    assert record["tier"] == "fillable"
    assert record["spec"] == {"headline": "Handcrafted since 1994"}


async def test_provenance_precedes_appspec_and_partial_failure_is_retryable():
    sbx = _FailAppSpecSandbox()
    assert (await _create_hello_app(sbx)).success
    before_spec = sbx._fs[APPSPEC_RELPATH]
    sbx.commits.clear()
    sbx.fail_appspec = True

    failed = await _add(sbx, "hello", {"headline": "Retry-safe"})

    assert not failed.success
    assert failed.error == "BATCH_PARTIAL_COMMIT"
    record_path = ".disco/primitives/hello.json"
    assert record_path in sbx._fs
    assert sbx.commits.index(record_path) < sbx.commits.index(APPSPEC_RELPATH)
    assert sbx._fs[APPSPEC_RELPATH] == before_spec
    assert record_path in [receipt.resource.identifier for receipt in failed.effect_receipts]

    sbx.fail_appspec = False
    sbx.commits.clear()
    repaired = await _add(sbx, "hello", {"headline": "Retry-safe"})
    assert repaired.success, repaired.content
    assert json.loads(sbx._fs[APPSPEC_RELPATH])["pages"][0]["title"] == "Retry-safe"


async def test_identical_reapply_is_loud_noop():
    sbx = FakeSandboxInstance()
    assert (await _create_hello_app(sbx)).success is True
    first = await _add(sbx, "hello", {"headline": "Once"})
    assert first.success is True
    again = await _add(sbx, "hello", {"headline": "Once"})
    assert again.success is False
    assert "no-op" in again.content


# ---- tier semantics ---------------------------------------------------------------


async def test_template_only_success_carries_spec_only_note():
    sbx = FakeSandboxInstance()
    assert (await _create_hello_app(sbx)).success is True
    out = await _add(sbx, _TPL_TEST_ID, {"headline": "Owned by Disco"})
    assert out.success is True, out.content
    assert out.structured is not None
    assert out.structured["tier"] == "template_only"
    assert "Disco-owned" in out.content
    assert "do not hand-edit" in out.content
