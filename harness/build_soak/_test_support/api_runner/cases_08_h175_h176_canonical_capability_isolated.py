"""Moved h175 h176 canonical capability isolated collection implementations."""

from __future__ import annotations

from ._shared import (
    _CID,
    DiscoApiClient,
    HttpTransport,
    assemble_dossier,
    asyncio,
    classify_dossier,
    clean_smoke_log,
    drive_scenario,
    httpx,
    json,
    pytest,
)
from .helpers_01 import (
    _CanonicalPreviewTransport,
    _plant_snapshot,
    _seed_db,
    _smoke_log_with_file_write,
    _smoke_scenario,
    _unverifiable_browser_observation,
)


def _capability_response(request, cid, bootstrap_path):
    assert request.headers["origin"] == "http://127.0.0.1:8000"
    assert request.headers["cookie"] == "disco_session=app-session-proof"
    assert request.headers["x-disco-csrf"] == "csrf-proof"
    assert json.loads(request.content) == {
        "target_path": "/",
        "transport": "canonical",
    }
    return httpx.Response(
        200,
        json={
            "bootstrap_url": f"http://127.0.0.2:19120{bootstrap_path}",
            "bootstrap_intent": "one-use-intent",
            "target_path": "/",
            "port": 5173,
            "transport": "canonical",
            "preview_authority": "live:proof",
        },
    )


def _bootstrap_response(request):
    assert request.url.host == "127.0.0.2"
    assert request.url.port == 19120
    assert request.headers["origin"] == "http://127.0.0.1:8000"
    assert "cookie" not in request.headers
    assert "authorization" not in request.headers
    assert "one-use-intent" not in str(request.url)
    assert request.content == b"intent=one-use-intent"
    return httpx.Response(
        200,
        headers={
            "set-cookie": (
                "disco_local_preview_19120=preview-proof; Path=/; HttpOnly; SameSite=Strict"
            )
        },
    )


def _preview_response(request):
    cookie = request.headers.get("cookie", "")
    assert request.url.host == "127.0.0.2"
    assert request.url.port == 19120
    assert "disco_local_preview_19120=preview-proof" in cookie
    assert "disco_session" not in cookie
    assert "one-use-intent" not in cookie
    assert "origin" not in request.headers
    return httpx.Response(200, text="<h1>Build Smoke OK</h1>")


@pytest.mark.asyncio
async def _impl_test_collect_preview_uses_canonical_capability_for_h175_unverifiable_case(tmp_path):
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    _plant_snapshot(proj, _CID, {"index.html": "<h1>STALE LOCAL SNAPSHOT</h1>"})
    _seed_db(db, _CID, [_unverifiable_browser_observation(1)])
    transport = _CanonicalPreviewTransport(
        db,
        states=["FINISHED"],
        preview_body="<h1>Build Smoke OK</h1>",
    )
    client = DiscoApiClient(
        transport, db_path=str(db), poll_interval_s=0.0, projects_root=str(proj)
    )

    preview = await client.collect_preview(_CID)

    assert preview == {
        "health": {"status": 200},
        "content": "<h1>Build Smoke OK</h1>",
        "available": True,
        "runtime_available": True,
        "runtime_availability_status": 200,
        "source": "isolated_path_capability",
    }
    assert transport.preview_fetches == 1


@pytest.mark.asyncio
async def _impl_test_collect_preview_never_masks_canonical_failure_with_snapshot(tmp_path):
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    _plant_snapshot(proj, _CID, {"index.html": "<h1>Build Smoke OK</h1>"})
    _seed_db(db, _CID, [])
    transport = _CanonicalPreviewTransport(
        db,
        states=["FINISHED"],
        preview_status=403,
        preview_body="preview capability required",
    )
    client = DiscoApiClient(
        transport, db_path=str(db), poll_interval_s=0.0, projects_root=str(proj)
    )

    preview = await client.collect_preview(_CID)

    assert preview["health"]["status"] == 403
    assert preview["content"] == "preview capability required"
    assert preview["available"] is False
    assert preview["source"] == "isolated_path_capability"


@pytest.mark.asyncio
async def _impl_test_collect_preview_retains_safe_failure_stage(tmp_path):
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, [])
    transport = _CanonicalPreviewTransport(
        db,
        states=["FINISHED"],
        preview_status=599,
        preview_body="isolated preview request failed",
        preview_failure_stage="fetch",
    )
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)

    preview = await client.collect_preview(_CID)

    assert preview["health"]["status"] == 599
    assert preview["failure_stage"] == "fetch"
    assert "capability" not in preview["content"]


@pytest.mark.asyncio
async def _impl_test_collect_preview_carries_canonical_wrong_body_without_forgery(tmp_path):
    db = tmp_path / "disco.db"
    _seed_db(db, _CID, [])
    transport = _CanonicalPreviewTransport(
        db,
        states=["FINISHED"],
        preview_body="<h1>WRONG CONTENT</h1>",
    )
    client = DiscoApiClient(transport, db_path=str(db), poll_interval_s=0.0)

    preview = await client.collect_preview(_CID)

    assert "Build Smoke OK" not in preview["content"]
    assert "WRONG CONTENT" in preview["content"]
    assert preview["source"] == "isolated_path_capability"


@pytest.mark.asyncio
async def _impl_test_http_transport_redeems_preview_capability_without_app_session():
    cid = "conv_a1b2c3d4proof"
    bootstrap_path = "/__disco/preview-auth"
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == f"/conversations/{cid}/preview/capability":
            return _capability_response(request, cid, bootstrap_path)
        if request.url.path == bootstrap_path:
            return _bootstrap_response(request)
        if request.url.path == "/":
            return _preview_response(request)
        if "/preview-app/" in request.url.path:
            raise AssertionError("legacy authenticated preview route was called")
        return httpx.Response(404)

    transport = HttpTransport(
        "http://127.0.0.1:8000",
        _transport=httpx.MockTransport(handler),
    )
    transport._cookie = "disco_session=app-session-proof"
    transport._csrf = "csrf-proof"

    status, body, _headers = await transport.fetch_isolated_preview(cid)

    assert status == 200
    assert body == "<h1>Build Smoke OK</h1>"
    assert seen == [f"/conversations/{cid}/preview/capability", bootstrap_path, "/"]


@pytest.mark.asyncio
async def _impl_test_http_transport_retains_safe_mint_stage_for_transport_failure():
    cid = "conv_a1b2c3d4proof"

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "connection failed for bearer=must-not-be-retained", request=request
        )

    transport = HttpTransport(
        "http://127.0.0.1:8000",
        _transport=httpx.MockTransport(handler),
    )
    transport._cookie = "disco_session=app-session-proof"
    transport._csrf = "csrf-proof"

    status, body, headers = await transport.fetch_isolated_preview(cid)

    assert (status, body, headers) == (599, "isolated preview request failed", {})
    assert transport.preview_failure_stage == "mint"
    assert "must-not-be-retained" not in body


@pytest.mark.asyncio
async def _impl_test_http_transport_classifies_generated_fetch_status_separately():
    cid = "conv_a1b2c3d4proof"
    bootstrap_path = "/__disco/preview-auth"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/preview/capability"):
            return _capability_response(request, cid, bootstrap_path)
        if request.url.path == bootstrap_path:
            return _bootstrap_response(request)
        if request.url.path == "/":
            return httpx.Response(503, text="preview temporarily unavailable")
        raise AssertionError(f"unexpected path {request.url.path}")

    transport = HttpTransport(
        "http://127.0.0.1:8000",
        _transport=httpx.MockTransport(handler),
    )
    transport._cookie = "disco_session=app-session-proof"
    transport._csrf = "csrf-proof"

    status, body, _headers = await transport.fetch_isolated_preview(cid)

    assert status == 503
    assert body == "preview temporarily unavailable"
    assert transport.preview_failure_stage == "fetch"


@pytest.mark.asyncio
async def _impl_test_http_transport_preview_stage_is_task_local_under_interleaving():
    first_cid = "conv_a1b2c3d4proof"
    second_cid = "conv_b1c2d3e4proof"
    first_capability_seen = asyncio.Event()
    release_first = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/preview/capability"):
            if first_cid in path:
                first_capability_seen.set()
                await release_first.wait()
                return httpx.Response(
                    200,
                    json={
                        "bootstrap_url": "http://127.0.0.2:19120/__disco/preview-auth",
                        "bootstrap_intent": "first-intent",
                        "target_path": "/",
                        "port": 5173,
                        "transport": "canonical",
                        "preview_authority": "live:first",
                    },
                )
            return httpx.Response(200, json={"target_path": "/"})
        if path == "/__disco/preview-auth":
            return httpx.Response(
                200,
                headers={
                    "set-cookie": (
                        "disco_local_preview_19120=first-proof; Path=/; HttpOnly; SameSite=Strict"
                    )
                },
            )
        if path == "/":
            raise httpx.ConnectError("fetch failed", request=request)
        raise AssertionError(f"unexpected path {path}")

    transport = HttpTransport(
        "http://127.0.0.1:8000",
        _transport=httpx.MockTransport(handler),
    )
    transport._cookie = "disco_session=app-session-proof"
    transport._csrf = "csrf-proof"

    async def fetch_with_stage(cid):
        result = await transport.fetch_isolated_preview(cid)
        return result, transport.preview_failure_stage

    first_task = asyncio.create_task(fetch_with_stage(first_cid))
    await asyncio.wait_for(first_capability_seen.wait(), timeout=1.0)
    second_result, second_stage = await asyncio.wait_for(fetch_with_stage(second_cid), timeout=1.0)
    release_first.set()
    first_result, first_stage = await asyncio.wait_for(first_task, timeout=1.0)

    assert second_result[0:2] == (502, "invalid preview capability response")
    assert first_result[0:2] == (599, "isolated preview request failed"), first_result
    assert second_stage == "validation"
    assert first_stage == "fetch"


@pytest.mark.asyncio
async def _impl_test_http_transport_completes_body_only_preview_storage_handoff():
    cid = "conv_a1b2c3d4proof"
    bootstrap_path = "/__disco/preview-auth"
    seen: list[tuple[str, bytes]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.content))
        if request.url.path.endswith("/preview/capability"):
            return httpx.Response(
                200,
                json={
                    "bootstrap_url": f"http://127.0.0.2:19120{bootstrap_path}",
                    "bootstrap_intent": "one-use-intent",
                    "target_path": "/",
                    "port": 8000,
                    "transport": "canonical",
                    "preview_authority": "committed:proof",
                },
            )
        if request.url.path == bootstrap_path and request.content == b"intent=one-use-intent":
            assert "cookie" not in request.headers
            return httpx.Response(
                200,
                text=(
                    '<script>fetch(location.pathname,{method:"POST",'
                    'body:"handoff=storage-handoff-proof"})</script>'
                ),
                headers={"clear-site-data": '"cache", "cookies", "storage"'},
            )
        if request.url.path == bootstrap_path:
            assert request.content == b"handoff=storage-handoff-proof"
            assert "cookie" not in request.headers
            return httpx.Response(
                200,
                json={"target": "/"},
                headers={
                    "set-cookie": (
                        "disco_local_preview_19120=preview-proof; Path=/; HttpOnly; SameSite=Strict"
                    )
                },
            )
        if request.url.path == "/":
            cookie = request.headers.get("cookie", "")
            assert "disco_local_preview_19120=preview-proof" in cookie
            assert "disco_session" not in cookie
            return httpx.Response(200, text="<h1>Build Smoke OK</h1>")
        raise AssertionError(f"unexpected path {request.url.path}")

    transport = HttpTransport(
        "http://127.0.0.1:8000",
        _transport=httpx.MockTransport(handler),
    )
    transport._cookie = "disco_session=app-session-proof"
    transport._csrf = "csrf-proof"

    status, body, _headers = await transport.fetch_isolated_preview(cid)

    assert status == 200
    assert body == "<h1>Build Smoke OK</h1>"
    assert [path for path, _content in seen] == [
        f"/conversations/{cid}/preview/capability",
        bootstrap_path,
        bootstrap_path,
        "/",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("document", "headers", "expected"),
    [
        (
            "<script>no body handoff</script>",
            {"clear-site-data": '"cookies"'},
            "invalid preview storage reset response",
        ),
        (
            '<script>body:"handoff=proof"</script>',
            {
                "clear-site-data": '"cookies"',
                "set-cookie": "disco_local_preview_19120=too-early; Path=/",
            },
            "preview storage reset installed a cookie too early",
        ),
    ],
)
async def _impl_test_http_transport_rejects_malformed_preview_storage_reset(
    document, headers, expected
):
    cid = "conv_a1b2c3d4proof"
    bootstrap_path = "/__disco/preview-auth"
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/preview/capability"):
            return httpx.Response(
                200,
                json={
                    "bootstrap_url": f"http://127.0.0.2:19120{bootstrap_path}",
                    "bootstrap_intent": "one-use-intent",
                    "target_path": "/",
                    "port": 8000,
                    "transport": "canonical",
                    "preview_authority": "committed:proof",
                },
            )
        if request.url.path == bootstrap_path:
            return httpx.Response(200, text=document, headers=headers)
        raise AssertionError("content loaded after malformed storage reset")

    transport = HttpTransport(
        "http://127.0.0.1:8000",
        _transport=httpx.MockTransport(handler),
    )
    transport._cookie = "disco_session=app-session-proof"
    transport._csrf = "csrf-proof"

    status, body, result_headers = await transport.fetch_isolated_preview(cid)

    assert status == 502
    assert body == expected
    assert result_headers == {}
    assert seen == [f"/conversations/{cid}/preview/capability", bootstrap_path]


@pytest.mark.asyncio
async def _impl_test_http_transport_does_not_reflect_failed_storage_handoff():
    cid = "conv_a1b2c3d4proof"
    bootstrap_path = "/__disco/preview-auth"
    handoff = "one-use-handoff-must-not-be-retained"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/preview/capability"):
            return httpx.Response(
                200,
                json={
                    "bootstrap_url": f"http://127.0.0.2:19120{bootstrap_path}",
                    "bootstrap_intent": "one-use-intent",
                    "target_path": "/",
                    "port": 8000,
                    "transport": "canonical",
                    "preview_authority": "committed:proof",
                },
            )
        if request.content == b"intent=one-use-intent":
            return httpx.Response(
                200,
                text=f'<script>body:"handoff={handoff}"</script>',
                headers={"clear-site-data": '"cookies"'},
            )
        return httpx.Response(409, text=f"hostile reflection {handoff}")

    transport = HttpTransport("http://127.0.0.1:8000", _transport=httpx.MockTransport(handler))
    transport._cookie = "disco_session=app-session-proof"
    transport._csrf = "csrf-proof"

    status, body, headers = await transport.fetch_isolated_preview(cid)

    assert status == 409
    assert body == "preview storage handoff failed"
    assert handoff not in body
    assert headers == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bootstrap_url",
    [
        "http://evil.example:19120/__disco/preview-auth",
        "http://user:pass@127.0.0.2:19120/__disco/preview-auth",
        "http://127.0.0.2:19120/__disco/preview-auth?intent=leak",
    ],
)
async def _impl_test_http_transport_rejects_malformed_preview_bootstrap_before_redemption(
    bootstrap_url,
):
    cid = "conv_a1b2c3d4proof"
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "bootstrap_url": bootstrap_url,
                "bootstrap_intent": "must-not-be-redeemed",
                "target_path": "/",
                "port": 8000,
                "transport": "canonical",
                "preview_authority": "committed:proof",
            },
        )

    transport = HttpTransport(
        "http://127.0.0.1:8000",
        _transport=httpx.MockTransport(handler),
    )
    transport._cookie = "disco_session=app-session-proof"
    transport._csrf = "csrf-proof"

    status, body, _headers = await transport.fetch_isolated_preview(cid)

    assert status == 502
    assert body == "invalid preview capability response"
    assert transport.preview_failure_stage == "validation"
    assert seen == [f"/conversations/{cid}/preview/capability"]


@pytest.mark.asyncio
async def _impl_test_http_transport_rejects_preview_bootstrap_that_sets_app_session():
    cid = "conv_a1b2c3d4proof"
    bootstrap_path = "/__disco/preview-auth"
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/preview/capability"):
            return httpx.Response(
                200,
                json={
                    "bootstrap_url": f"http://127.0.0.2:19120{bootstrap_path}",
                    "bootstrap_intent": "one-use-intent",
                    "target_path": "/",
                    "port": 8000,
                    "transport": "canonical",
                    "preview_authority": "committed:proof",
                },
            )
        if request.url.path == bootstrap_path:
            return httpx.Response(
                200,
                headers=[
                    ("set-cookie", "disco_local_preview_19120=preview-proof; Path=/"),
                    ("set-cookie", "disco_session=must-not-cross; Path=/"),
                ],
            )
        raise AssertionError("generated content loaded after app-session crossover")

    transport = HttpTransport(
        "http://127.0.0.1:8000",
        _transport=httpx.MockTransport(handler),
    )
    transport._cookie = "disco_session=app-session-proof"
    transport._csrf = "csrf-proof"

    status, body, _headers = await transport.fetch_isolated_preview(cid)

    assert status == 502
    assert body == "preview bootstrap crossed application session"
    assert seen == [f"/conversations/{cid}/preview/capability", bootstrap_path]


@pytest.mark.asyncio
async def _impl_test_http_transport_never_retains_intent_reflected_by_failed_redemption():
    cid = "conv_a1b2c3d4proof"
    bootstrap_path = "/__disco/preview-auth"
    intent = "one-use-intent-must-not-be-retained"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/preview/capability"):
            return httpx.Response(
                200,
                json={
                    "bootstrap_url": f"http://127.0.0.2:19120{bootstrap_path}",
                    "bootstrap_intent": intent,
                    "target_path": "/",
                    "port": 8000,
                    "transport": "canonical",
                    "preview_authority": "committed:proof",
                },
            )
        if request.url.path == bootstrap_path:
            return httpx.Response(403, text=f"hostile reflection {intent}")
        raise AssertionError("content fetch ran after failed redemption")

    transport = HttpTransport(
        "http://127.0.0.1:8000",
        _transport=httpx.MockTransport(handler),
    )
    transport._cookie = "disco_session=app-session-proof"
    transport._csrf = "csrf-proof"

    status, body, headers = await transport.fetch_isolated_preview(cid)

    assert status == 403
    assert body == "preview capability redemption failed"
    assert transport.preview_failure_stage == "redemption"
    assert intent not in body
    assert headers == {}


@pytest.mark.asyncio
async def _impl_test_static_build_classifies_pass_with_canonical_preview_and_snapshot_workspace(
    tmp_path,
):
    # Workspace truth remains the durable ProjectStore snapshot. Preview truth is
    # independently fetched through the product's isolated capability boundary.
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    _seed_db(db, _CID, clean_smoke_log())
    _plant_snapshot(proj, _CID, {"index.html": "<h1>Build Smoke OK</h1>"})
    transport = _CanonicalPreviewTransport(
        db,
        states=["AWAITING_PLAN_APPROVAL", "FINISHED", "FINISHED", "FINISHED"],
        preview_body="<h1>Build Smoke OK</h1>",
    )
    client = DiscoApiClient(
        transport, db_path=str(db), poll_interval_s=0.0, projects_root=str(proj)
    )
    scenario = _smoke_scenario()
    run = await drive_scenario(client, scenario, model="m", autonomous=False, timeout_s=5)
    base = assemble_dossier(
        tmp_path / "out", "run_b10_pass", scenario, run, model="m", autonomous=False
    )
    classification = classify_dossier(base, scenario, run, autonomous=False)
    assert classification["status"] == "PASS", classification


@pytest.mark.asyncio
async def _impl_test_preview_required_contract_rejects_unrecorded_or_local_fallback_provenance(
    tmp_path,
):
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    _seed_db(db, _CID, clean_smoke_log())
    _plant_snapshot(proj, _CID, {"index.html": "<h1>Build Smoke OK</h1>"})
    transport = _CanonicalPreviewTransport(
        db,
        states=["AWAITING_PLAN_APPROVAL", "FINISHED", "FINISHED", "FINISHED"],
        preview_body="<h1>Build Smoke OK</h1>",
    )
    client = DiscoApiClient(
        transport, db_path=str(db), poll_interval_s=0.0, projects_root=str(proj)
    )
    scenario = _smoke_scenario()
    run = await drive_scenario(client, scenario, model="m", autonomous=False, timeout_s=5)
    assert run.preview is not None
    run.preview["source"] = "snapshot_serve_probe"
    base = assemble_dossier(
        tmp_path / "out", "run_untrusted_preview_source", scenario, run, model="m", autonomous=False
    )

    classification = classify_dossier(base, scenario, run, autonomous=False)

    assert classification["status"] == "INVALID_RUN"
    assert classification["code"] == "SCENARIO_CONTRACT_UNSATISFIABLE"
    assert classification["first_broken_link"] == "scenario_contract -> required_evidence"


@pytest.mark.asyncio
async def _impl_test_canonical_preview_does_not_mask_wrong_content(tmp_path):
    db = tmp_path / "disco.db"
    proj = tmp_path / "projects"
    # The agent actually WROTE the wrong content (a file_write of "<h1>WRONG</h1>"), so the
    # capture is PROVEN (raw_sha): a proven wrong deliverable stays a hard ARTIFACT_TRUTH_MISMATCH
    # under the Part A proof-fold — it is NOT downgraded to an unverified-snapshot INVALID_RUN.
    _seed_db(db, _CID, _smoke_log_with_file_write("index.html", "<h1>WRONG</h1>"))
    _plant_snapshot(proj, _CID, {"index.html": "<h1>WRONG</h1>"})  # missing the needle
    transport = _CanonicalPreviewTransport(
        db,
        states=["AWAITING_PLAN_APPROVAL", "FINISHED", "FINISHED", "FINISHED"],
        preview_body="<h1>WRONG</h1>",
    )
    client = DiscoApiClient(
        transport, db_path=str(db), poll_interval_s=0.0, projects_root=str(proj)
    )
    scenario = _smoke_scenario()
    run = await drive_scenario(client, scenario, model="m", autonomous=False, timeout_s=5)
    base = assemble_dossier(
        tmp_path / "out", "run_b10_wrong", scenario, run, model="m", autonomous=False
    )
    classification = classify_dossier(base, scenario, run, autonomous=False)
    assert classification["status"] == "FAIL"
    assert classification["code"] in ("ARTIFACT_TRUTH_MISMATCH", "PREVIEW_TRUTH_MISMATCH"), (
        classification
    )
