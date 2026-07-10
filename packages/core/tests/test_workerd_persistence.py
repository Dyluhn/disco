from __future__ import annotations

import json
import socket
import uuid

import pytest
from disco.core.appkit import default_lead_gen_app_spec, generate, get_recipe

from _workerd_harness import WorkerdApp, wrangler_available

pytestmark = pytest.mark.integration

if not wrangler_available():
    pytest.skip("wrangler is required for the local workerd persistence proof", allow_module_level=True)


def test_lead_survives_worker_cold_restart() -> None:
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    design = recipe.to_design_spec()
    app = default_lead_gen_app_spec("Acme", recipe)
    tree = generate(app, design)
    marker = f"survive the restart {uuid.uuid4().hex}"
    port = _free_port()

    with WorkerdApp(tree, admin_token="testtoken123") as w:
        w.boot(port=port)

        unauth_status, _ = w.get("/api/leads")
        print(f"before restart unauth GET /api/leads -> {unauth_status}")
        assert unauth_status == 401

        post_status, post_body = w.post_json(
            "/api/leads",
            {
                "name": "Runtime Proof",
                "email": f"proof-{uuid.uuid4().hex}@example.com",
                "message": marker,
            },
        )
        print(f"POST /api/leads marker={marker!r} -> {post_status} {post_body}")
        assert post_status == 201
        assert json.loads(post_body) == {"ok": True}

        visible_status, visible_body = w.get("/api/leads", token="testtoken123")
        print(
            "before restart authed GET /api/leads "
            f"-> {visible_status}; marker present={marker in visible_body}"
        )
        assert visible_status == 200
        assert marker in visible_body

        w.kill()
        assert w.is_down()
        print(f"killed wrangler dev on port {port}; port down={w.is_down()}")

        w.boot(port=port)

        restarted_unauth_status, _ = w.get("/api/leads")
        print(f"after restart unauth GET /api/leads -> {restarted_unauth_status}")
        assert restarted_unauth_status == 401

        restarted_status, restarted_body = w.get("/api/leads", token="testtoken123")
        print(
            "after restart authed GET /api/leads "
            f"-> {restarted_status}; marker present={marker in restarted_body}"
        )
        assert restarted_status == 200
        assert marker in restarted_body


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        assert isinstance(port, int)
        return port
