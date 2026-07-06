from __future__ import annotations

import hashlib
import json
import socket
import sqlite3
import urllib.error
import urllib.request
import uuid

import pytest
from _workerd_harness import WorkerdApp, wrangler_available
from disco.core.appkit import (
    RECORDS_PRIMITIVE_ID,
    default_records_app_spec,
    default_records_auth_app_spec,
    generate,
    get_recipe,
)
from disco.core.appkit.spec import AppSpec, Entity, EntityField

_RECORDS_BW2_DIGEST = "d2e6009449a482fc356416990c723c298358e16925fc829d6a2af252f727bb11"
_AUTH_RECORDS_WORKER_SCHEMA_DIGEST = (
    "b989c72e80629609bac03d9024f6544515a35762a294b6bdacdaedb100605e67"
)


def _design():
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    return recipe.to_design_spec()


def _recipe():
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    return recipe


def _records_tree() -> dict[str, str]:
    recipe = _recipe()
    return generate(default_records_app_spec("Team Ops", recipe), recipe.to_design_spec())


def _auth_tree() -> dict[str, str]:
    recipe = _recipe()
    return generate(default_records_auth_app_spec("Shift Calendar", recipe), recipe.to_design_spec())


def _digest(tree: dict[str, str]) -> str:
    h = hashlib.sha256()
    for path in sorted(tree):
        h.update(path.encode("utf-8"))
        h.update(b"\0")
        h.update(tree[path].encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def _digest_paths(tree: dict[str, str], paths: tuple[str, ...]) -> str:
    h = hashlib.sha256()
    for path in paths:
        h.update(path.encode("utf-8"))
        h.update(b"\0")
        h.update(tree[path].encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def test_no_auth_records_app_is_byte_identical_to_bw2_default() -> None:
    assert _digest(_records_tree()) == _RECORDS_BW2_DIGEST


def test_auth_records_emits_auth_schema_worker_and_approval_rbac() -> None:
    tree = _auth_tree()
    schema = tree["schema.sql"]
    assert 'CREATE TABLE IF NOT EXISTS "users"' in schema
    assert 'CREATE TABLE IF NOT EXISTS "sessions"' in schema
    assert '"email" TEXT NOT NULL UNIQUE' in schema
    assert 'FOREIGN KEY("user_id") REFERENCES "users"("id")' in schema
    assert '"status"' not in schema

    drizzle = tree["src/db/schema.ts"]
    assert 'export const users = sqliteTable("users"' in drizzle
    assert 'export const sessions = sqliteTable("sessions"' in drizzle
    assert ".references(() => users.id)" in drizzle
    assert 'status: text("status")' not in drizzle

    worker = tree["worker/index.ts"]
    assert 'rawPath === "/api/register"' in worker
    assert 'rawPath === "/api/login"' in worker
    assert 'rawPath === "/api/logout"' in worker
    assert "function sameOriginOk(request: Request, url: URL)" in worker
    assert 'return json({ error: "bad origin" }, 403)' in worker
    assert "function requireJsonContentType(request: Request)" in worker
    assert 'return json({ error: "unsupported media type" }, 415)' in worker
    assert "const MAX_PASSWORD_LEN = 1024" in worker
    assert 'return { ok: false, error: "password too long" }' in worker
    assert "spendInvalidLoginWork(check.password)" in worker
    assert "const DUMMY_LOGIN_HASH_HEX" in worker
    assert "async function isAdminAuthorized" in worker
    assert "sha256Hex(header.slice(prefix.length))" in worker
    assert "fixedWorkHexEqual(presentedHash, expectedHash)" in worker
    assert "fixedWorkStringEqual" not in worker
    assert "rawPathname(request.url, url.origin)" in worker
    assert "decodedPathname(rawPath)" in worker
    assert "decodedPath.startsWith(\"/api/\")" in worker
    assert 'return json({ error: "not found" }, 404)' in worker
    assert "deriveBits" in worker
    assert "crypto.getRandomValues" in worker
    assert "HttpOnly; SameSite=Strict; Secure; Path=/" in worker
    approval_route = worker[worker.index('"/api/approval"') : worker.index('"/api/approval"') + 260]
    assert 'writeRoles: ["approver"]' in approval_route
    assert "authorizeSession(session, route.writeRoles)" in worker

    guide = tree["OWNER_GUIDE.md"]
    assert "## Authorization scope" in guide
    assert "does not yet enforce per-row ownership" in guide

    wrangler = tree["wrangler.toml"]
    assert "run_worker_first = true" in wrangler


def test_auth_records_worker_and_schema_outputs_stay_stable() -> None:
    assert _digest_paths(
        _auth_tree(),
        ("schema.sql", "migrations/0001_init.sql", "worker/index.ts", "src/db/schema.ts"),
    ) == _AUTH_RECORDS_WORKER_SCHEMA_DIGEST


def test_auth_records_schema_sql_round_trips_with_fk_enforcement() -> None:
    con = sqlite3.connect(":memory:")
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.executescript(_auth_tree()["schema.sql"])
        con.execute(
            'INSERT INTO "users" ("email", "password_hash", "password_salt", "role") '
            "VALUES (?, ?, ?, ?)",
            ("approver@example.com", "hash", "salt", "approver"),
        )
        con.execute(
            'INSERT INTO "sessions" ("token_hash", "user_id", "expires_at") VALUES (?, ?, ?)',
            ("tokenhash", 1, "2026-07-13T00:00:00.000Z"),
        )
        con.execute(
            'INSERT INTO "team_member" ("name", "email") VALUES (?, ?)',
            ("Ada", "ada@example.com"),
        )
        con.execute(
            'INSERT INTO "time_off_request" ("member_id", "reason") VALUES (?, ?)',
            (1, "Vacation"),
        )
        con.execute(
            'INSERT INTO "approval" ("request_id", "decision") VALUES (?, ?)',
            (1, "approved"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                'INSERT INTO "sessions" ("token_hash", "user_id", "expires_at") '
                "VALUES (?, ?, ?)",
                ("badtokenhash", 999, "2026-07-13T00:00:00.000Z"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                'INSERT INTO "approval" ("request_id", "decision") VALUES (?, ?)',
                (999, "approved"),
            )
    finally:
        con.close()


def test_records_role_validators_require_declared_roles() -> None:
    with pytest.raises(ValueError, match="unknown role"):
        AppSpec(
            schema_version=1,
            app_kind=RECORDS_PRIMITIVE_ID,
            name="Bad",
            roles=("member",),
            entities=(
                Entity(
                    id="approval",
                    name="Approval",
                    write_roles=("approver",),
                    fields=(EntityField(name="decision", type="str"),),
                ),
            ),
        )

    with pytest.raises(ValueError, match="AppSpec.roles is empty"):
        AppSpec(
            schema_version=1,
            app_kind=RECORDS_PRIMITIVE_ID,
            name="Bad",
            entities=(
                Entity(
                    id="approval",
                    name="Approval",
                    write_roles=("approver",),
                    fields=(EntityField(name="decision", type="str"),),
                ),
            ),
        )


@pytest.mark.integration
def test_rbac_approver_only_and_survives_restart() -> None:
    if not wrangler_available():
        pytest.skip("wrangler is required for the local workerd records auth proof")

    tree = generate(default_records_auth_app_spec("Shift Calendar", _recipe()), _design())
    admin_token = "recordadmintoken123"
    port = _free_port()
    suffix = uuid.uuid4().hex
    member_email = f"member-{suffix}@example.com"
    approver_email = f"approver-{suffix}@example.com"
    member_password = "member-password-123"
    approver_password = "approver-password-123"

    with WorkerdApp(tree, admin_token=admin_token) as w:
        w.boot(port=port)
        member_cookies = w.cookie_jar()
        approver_cookies = w.cookie_jar()
        empty_cookies = w.cookie_jar()

        status, body = w.post_json(
            "/api/register",
            {"email": approver_email, "password": approver_password, "role": "approver"},
            token=admin_token,
        )
        print(f"register approver -> {status} {body}")
        assert status == 201

        status, body = w.post_json(
            "/api/register",
            {"email": member_email, "password": member_password, "role": "member"},
            token=admin_token,
        )
        print(f"register member -> {status} {body}")
        assert status == 201

        status, body = w.post_json(
            "/api/login",
            {"email": member_email, "password": member_password},
            cookie_jar=member_cookies,
        )
        print(f"login member -> {status} {body}")
        assert status == 200

        status, body = w.post_json(
            "/api/login",
            {"email": approver_email, "password": approver_password},
            cookie_jar=approver_cookies,
        )
        print(f"login approver -> {status} {body}")
        assert status == 200

        bad_type_status, bad_type_body = _raw_request(
            port,
            "POST",
            "/api/login",
            body=b"not-json",
            headers={"Content-Type": "text/plain"},
        )
        print(f"bad content-type POST /api/login -> {bad_type_status} {bad_type_body}")
        assert bad_type_status == 415
        assert json.loads(bad_type_body) == {"error": "unsupported media type"}

        bad_origin_status, bad_origin_body = _raw_request(
            port,
            "POST",
            "/api/login",
            body=json.dumps(
                {"email": member_email, "password": member_password}
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Origin": "https://evil.example",
            },
        )
        print(f"bad origin POST /api/login -> {bad_origin_status} {bad_origin_body}")
        assert bad_origin_status == 403
        assert json.loads(bad_origin_body) == {"error": "bad origin"}

        logout_headers = {"Origin": "https://evil.example"}
        member_cookie = member_cookies.header()
        assert member_cookie is not None
        logout_headers["Cookie"] = member_cookie
        logout_status, logout_body = _raw_request(
            port,
            "POST",
            "/api/logout",
            headers=logout_headers,
        )
        print(f"bad origin POST /api/logout -> {logout_status} {logout_body}")
        assert logout_status == 403
        assert json.loads(logout_body) == {"error": "bad origin"}

        long_password_status, long_password_body = _raw_request(
            port,
            "POST",
            "/api/login",
            body=json.dumps({"email": member_email, "password": "x" * 1025}).encode(
                "utf-8"
            ),
            headers={"Content-Type": "application/json"},
        )
        print(
            "long password POST /api/login -> "
            f"{long_password_status} {long_password_body}"
        )
        assert long_password_status == 401
        assert json.loads(long_password_body) == {"error": "invalid credentials"}

        encoded_status, encoded_body = _raw_request(port, "GET", "/api%2Fapproval")
        print(f"GET /api%2Fapproval encoded API path -> {encoded_status} {encoded_body}")
        assert encoded_status == 404
        assert json.loads(encoded_body) == {"error": "not found"}

        head_status, head_body = _raw_request(port, "HEAD", "/api/approval")
        print(f"HEAD /api/approval method mismatch -> {head_status} {head_body}")
        assert head_status == 404

        put_status, put_body = _raw_request(
            port,
            "PUT",
            "/api/approval",
            body=b"{}",
            headers={"Content-Type": "application/json"},
        )
        print(f"PUT /api/approval method mismatch -> {put_status} {put_body}")
        assert put_status == 404
        assert json.loads(put_body) == {"error": "not found"}

        status, body = w.post_json(
            "/api/team_member",
            {"name": "Member One", "email": member_email},
            cookie_jar=member_cookies,
        )
        print(f"member creates team_member -> {status} {body}")
        assert status == 201

        status, body = w.post_json(
            "/api/time_off_request",
            {"member_id": 1, "reason": "PTO"},
            cookie_jar=member_cookies,
        )
        print(f"member creates time_off_request -> {status} {body}")
        assert status == 201

        member_status, member_body = w.post_json(
            "/api/approval",
            {"request_id": 1, "decision": "approved-by-member"},
            cookie_jar=member_cookies,
        )
        approver_status, approver_body = w.post_json(
            "/api/approval",
            {"request_id": 1, "decision": "approved-before-restart"},
            cookie_jar=approver_cookies,
        )
        unauth_status, unauth_body = w.post_json(
            "/api/approval",
            {"request_id": 1, "decision": "approved-unauth"},
            cookie_jar=empty_cookies,
        )
        print(
            "before cold restart: "
            f"approver={approver_status} {approver_body}; "
            f"member={member_status} {member_body}; "
            f"unauth={unauth_status} {unauth_body}"
        )
        assert member_status == 403
        assert approver_status == 201
        assert unauth_status == 401

        bad_status, bad_body = w.post_json(
            "/api/login",
            {"email": member_email, "password": "wrong-password-123"},
            cookie_jar=w.cookie_jar(),
        )
        unknown_status, unknown_body = w.post_json(
            "/api/login",
            {"email": f"missing-{suffix}@example.com", "password": "wrong-password-123"},
            cookie_jar=w.cookie_jar(),
        )
        print(
            "bad login comparison: "
            f"wrong_password={bad_status} {bad_body}; "
            f"unknown_email={unknown_status} {unknown_body}"
        )
        assert bad_status == 401
        assert unknown_status == 401
        assert json.loads(bad_body) == {"error": "invalid credentials"}
        assert bad_body == unknown_body

        w.kill()
        assert w.is_down()
        print(f"killed wrangler dev on port {port}; port down={w.is_down()}")

        w.boot(port=port)

        restart_approver_status, restart_approver_body = w.post_json(
            "/api/approval",
            {"request_id": 1, "decision": "approved-after-restart"},
            cookie_jar=approver_cookies,
        )
        restart_member_status, restart_member_body = w.post_json(
            "/api/approval",
            {"request_id": 1, "decision": "member-after-restart"},
            cookie_jar=member_cookies,
        )
        restart_unauth_status, restart_unauth_body = w.post_json(
            "/api/approval",
            {"request_id": 1, "decision": "unauth-after-restart"},
            cookie_jar=w.cookie_jar(),
        )
        print(
            "after cold restart with persisted session: "
            f"approver={restart_approver_status} {restart_approver_body}; "
            f"member={restart_member_status} {restart_member_body}; "
            f"unauth={restart_unauth_status} {restart_unauth_body}"
        )
        assert restart_approver_status == 201
        assert restart_member_status == 403
        assert restart_unauth_status == 401


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        assert isinstance(port, int)
        return port


def _raw_request(
    port: int,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, str]:
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(
        url, data=body, headers=dict(headers or {}), method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=10.0) as res:
            return res.status, res.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
