from __future__ import annotations

import json
import socket
import sqlite3
import subprocess
import urllib.error
import urllib.request
import uuid

import pytest
from _workerd_harness import WorkerdApp
from disco.core.appkit import RECORDS_PRIMITIVE_ID, generate, get_recipe, records_verify
from disco.core.appkit.form_primitive import FormSpec, apply_form_spec
from disco.core.appkit.spec import (
    AppSpec,
    Entity,
    EntityField,
    Page,
    RecordPolicy,
    Section,
    SectionContent,
)


def _recipe():
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    return recipe


def _forum_app() -> AppSpec:
    return AppSpec(
        schema_version=1,
        app_kind=RECORDS_PRIMITIVE_ID,
        name="Common Ground",
        roles=("member", "moderator", "administrator"),
        role_admin_roles=("administrator",),
        pages=(
            Page(
                id="home",
                route="/",
                title="Common Ground",
                sections=(
                    Section(
                        id="hero",
                        kind="hero",
                        content=SectionContent(
                            heading="Common Ground",
                            subheading="A persistent community forum.",
                        ),
                    ),
                ),
            ),
        ),
        entities=(
            Entity(
                id="category",
                name="Category",
                fields=(
                    EntityField(name="name", type="str", required=True),
                    EntityField(name="description", type="text", required=True),
                ),
                record_policy=RecordPolicy(
                    public_read=True,
                    create_roles=("administrator",),
                    manage_roles=("administrator",),
                ),
            ),
            Entity(
                id="thread",
                name="Thread",
                fields=(
                    EntityField(
                        name="category_id", type="int", required=True, references="category"
                    ),
                    EntityField(name="title", type="str", required=True),
                    EntityField(name="body", type="text", required=True),
                ),
                record_policy=RecordPolicy(
                    public_read=True,
                    create_roles=("member", "moderator", "administrator"),
                    owner_managed=True,
                    manage_roles=("moderator", "administrator"),
                    lock_roles=("moderator", "administrator"),
                ),
            ),
            Entity(
                id="reply",
                name="Reply",
                fields=(
                    EntityField(name="thread_id", type="int", required=True, references="thread"),
                    EntityField(name="body", type="text", required=True),
                ),
                record_policy=RecordPolicy(
                    public_read=True,
                    create_roles=("member", "moderator", "administrator"),
                    owner_managed=True,
                    manage_roles=("moderator", "administrator"),
                    parent_lock_field="thread_id",
                ),
            ),
        ),
    )


def _tree() -> dict[str, str]:
    recipe = _recipe()
    return generate(_forum_app(), recipe.to_design_spec())


def _tree_with_form() -> dict[str, str]:
    recipe = _recipe()
    app = apply_form_spec(
        _forum_app(),
        FormSpec.model_validate(
            {
                "form_id": "contact_request",
                "title": "Contact the team",
                "fields": [{"name": "email", "label": "Email", "kind": "email", "required": True}],
                "success_message": "Thanks. We will reply soon.",
            }
        ),
    )
    return generate(app, recipe.to_design_spec())


def test_policy_records_generate_server_owned_schema_worker_and_real_ui() -> None:
    tree = _tree()
    schema = tree["schema.sql"]
    assert '"owner_user_id" INTEGER NOT NULL' in schema
    assert 'FOREIGN KEY("owner_user_id") REFERENCES "users"("id")' in schema
    assert '"locked" INTEGER NOT NULL DEFAULT 0' in schema

    worker = tree["worker/index.ts"]
    assert "session.userId" in worker
    assert 'rawPath === "/api/session"' in worker
    assert "canManageRow(session, meta, row)" in worker
    assert 'return json({ error: "parent record is locked" }, 409)' in worker
    assert "userRoleMatch" in worker
    assert "api\\/users\\/([1-9][0-9]*)\\/role" in worker
    assert 'request.headers.get("X-Role")' not in worker

    ui = tree["src/components/RecordsWorkspace.tsx"]
    assert "Permissions are enforced by the server from your signed session." in ui
    assert "Sign in" in ui
    assert "User roles" in ui
    assert "owner_user_id" not in json.dumps(_forum_app().model_dump(mode="json"))

    folded = _tree_with_form()
    assert "function requireJsonContentType" in folded["worker/index.ts"]
    assert "APP_FORM_ROUTES" in folded["worker/index.ts"]
    assert "contact_request" not in folded["src/components/RecordsWorkspace.tsx"]


def test_policy_records_schema_executes_with_owned_relations() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(_tree()["schema.sql"])
        connection.execute(
            "INSERT INTO users (email, password_hash, password_salt, role) VALUES (?, ?, ?, ?)",
            ("member@example.com", "hash", "salt", "member"),
        )
        connection.execute(
            "INSERT INTO category (name, description) VALUES (?, ?)",
            ("General", "General discussion"),
        )
        connection.execute(
            "INSERT INTO thread (category_id, title, body, owner_user_id) VALUES (?, ?, ?, ?)",
            (1, "Welcome", "Hello", 1),
        )
        connection.execute(
            "INSERT INTO reply (thread_id, body, owner_user_id) VALUES (?, ?, ?)",
            (1, "First reply", 1),
        )
        assert connection.execute("SELECT locked FROM thread").fetchone() == (0,)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO reply (thread_id, body, owner_user_id) VALUES (?, ?, ?)",
                (999, "orphan", 1),
            )
    finally:
        connection.close()


def test_policy_validation_rejects_ambiguous_or_unenforceable_specs() -> None:
    data = _forum_app().model_dump(mode="json")
    data["entities"][1]["write_roles"] = ["member"]
    with pytest.raises(ValueError, match="record_policy owns write authorization"):
        from disco.core.appkit import prepare_records_app_spec

        prepare_records_app_spec(AppSpec.model_validate(data))

    data = _forum_app().model_dump(mode="json")
    data["entities"][2]["record_policy"]["parent_lock_field"] = "body"
    with pytest.raises(ValueError, match="must name a foreign-key field"):
        AppSpec.model_validate(data)

    data = _forum_app().model_dump(mode="json")
    data["entities"][1]["record_policy"]["create_roles"] = ["outsider"]
    with pytest.raises(ValueError, match="unknown role"):
        AppSpec.model_validate(data)

    data = _forum_app().model_dump(mode="json")
    data["entities"][2]["record_policy"]["parent_lock_field"] = "thread-id"
    with pytest.raises(ValueError, match="snake_case identifier"):
        AppSpec.model_validate(data)

    data = _forum_app().model_dump(mode="json")
    data["app_kind"] = "lead_gen"
    with pytest.raises(ValueError, match="require app_kind='records'"):
        AppSpec.model_validate(data)

    data = _forum_app().model_dump(mode="json")
    data["entities"][0].pop("record_policy")
    with pytest.raises(ValueError, match="every persistent entity"):
        from disco.core.appkit import prepare_records_app_spec

        prepare_records_app_spec(AppSpec.model_validate(data))


def test_records_verifier_is_artifact_derived_and_fails_tampering() -> None:
    app = _forum_app()
    design = _recipe().to_design_spec()
    tree = generate(app, design)
    result = records_verify(app, design, tree)
    assert result.ok is True
    assert {check.name for check in result.checks} == {
        "records_schema",
        "records_drizzle_contract",
        "records_worker_contract",
        "records_policy_ui",
        "records_policy_shell",
    }

    tampered = dict(tree)
    tampered["worker/index.ts"] = tampered["worker/index.ts"].replace(
        "session.userId", "Number((body as Record<string, unknown>).owner_user_id)"
    )
    failed = records_verify(app, design, tampered)
    assert failed.ok is False
    assert (
        next(check for check in failed.checks if check.name == "records_worker_contract").passed
        is False
    )


@pytest.mark.integration
def test_policy_records_role_matrix_locking_and_persistence() -> None:
    admin_token = "records-policy-admin-token"
    port = _free_port()
    suffix = uuid.uuid4().hex
    credentials = {
        "admin": (f"admin-{suffix}@example.com", "admin-password-123", "administrator"),
        "moderator": (f"moderator-{suffix}@example.com", "moderator-password-123", "moderator"),
        "alice": (f"alice-{suffix}@example.com", "alice-password-123", "member"),
        "bob": (f"bob-{suffix}@example.com", "bob-password-123", "member"),
    }

    with WorkerdApp(_tree_with_form(), admin_token=admin_token) as worker:
        build = subprocess.run(
            ["npm", "run", "build"],
            cwd=worker.app_dir,
            text=True,
            capture_output=True,
            timeout=180,
            check=False,
        )
        assert build.returncode == 0, build.stdout + build.stderr
        worker.boot(port=port)
        jars = {name: worker.cookie_jar() for name in credentials}
        for name, (email, password, role) in credentials.items():
            status, _ = worker.post_json(
                "/api/register",
                {"email": email, "password": password, "role": role},
                token=admin_token,
            )
            assert status == 201, name
            status, _ = worker.post_json(
                "/api/login", {"email": email, "password": password}, cookie_jar=jars[name]
            )
            assert status == 200, name

        assert _request_json(port, "GET", "/api/category")[0] == 200
        assert (
            _request_json(
                port,
                "POST",
                "/api/thread",
                body={"category_id": 1, "title": "guest", "body": "denied"},
            )[0]
            == 401
        )

        assert (
            _request_json(
                port,
                "POST",
                "/api/category",
                body={"name": "General", "description": "General discussion"},
                cookie=jars["alice"].header(),
            )[0]
            == 403
        )
        assert (
            _request_json(
                port,
                "POST",
                "/api/category",
                body={"name": "General", "description": "General discussion"},
                cookie=jars["admin"].header(),
            )[0]
            == 201
        )

        forged_status, _ = _request_json(
            port,
            "POST",
            "/api/thread",
            body={
                "category_id": 1,
                "title": "Forged",
                "body": "Nope",
                "owner_user_id": 999,
            },
            cookie=jars["alice"].header(),
            headers={"X-Role": "administrator"},
        )
        assert forged_status == 400
        assert (
            _request_json(
                port,
                "POST",
                "/api/thread",
                body={"category_id": 1, "title": "Welcome", "body": "Hello"},
                cookie=jars["alice"].header(),
            )[0]
            == 201
        )

        thread_status, thread_body = _request_json(port, "GET", "/api/thread")
        assert thread_status == 200
        thread = thread_body["thread"][0]
        thread_id = int(thread["id"])
        alice_session = _request_json(port, "GET", "/api/session", cookie=jars["alice"].header())[1]
        assert thread["owner_user_id"] == alice_session["user_id"]

        assert (
            _request_json(
                port,
                "PATCH",
                f"/api/thread/{thread_id}",
                body={"title": "stolen", "body": "stolen", "category_id": 1},
                cookie=jars["bob"].header(),
                headers={"X-Role": "administrator"},
            )[0]
            == 403
        )
        assert (
            _request_json(
                port,
                "PATCH",
                f"/api/thread/{thread_id}",
                body={"title": "Owner edit", "body": "Updated", "category_id": 1},
                cookie=jars["alice"].header(),
            )[0]
            == 200
        )
        assert (
            _request_json(
                port,
                "PATCH",
                f"/api/thread/{thread_id}",
                body={"title": "Moderated", "body": "Updated", "category_id": 1},
                cookie=jars["moderator"].header(),
            )[0]
            == 200
        )

        assert (
            _request_json(
                port, "POST", f"/api/thread/{thread_id}/lock", cookie=jars["alice"].header()
            )[0]
            == 403
        )
        assert (
            _request_json(
                port,
                "POST",
                f"/api/thread/{thread_id}/lock",
                cookie=jars["moderator"].header(),
            )[0]
            == 200
        )
        assert (
            _request_json(
                port,
                "POST",
                "/api/reply",
                body={"thread_id": thread_id, "body": "blocked while locked"},
                cookie=jars["alice"].header(),
            )[0]
            == 409
        )
        assert (
            _request_json(
                port,
                "POST",
                f"/api/thread/{thread_id}/unlock",
                cookie=jars["moderator"].header(),
            )[0]
            == 200
        )
        assert (
            _request_json(
                port,
                "POST",
                "/api/reply",
                body={"thread_id": thread_id, "body": "Now open"},
                cookie=jars["alice"].header(),
            )[0]
            == 201
        )

        users = _request_json(port, "GET", "/api/users", cookie=jars["admin"].header())[1]["users"]
        bob_id = next(int(user["id"]) for user in users if user["email"] == credentials["bob"][0])
        assert (
            _request_json(
                port,
                "PATCH",
                f"/api/users/{bob_id}/role",
                body={"role": "moderator"},
                cookie=jars["alice"].header(),
                headers={"X-Role": "administrator"},
            )[0]
            == 403
        )
        assert (
            _request_json(
                port,
                "PATCH",
                f"/api/users/{bob_id}/role",
                body={"role": "moderator"},
                cookie=jars["admin"].header(),
            )[0]
            == 200
        )
        assert (
            _request_json(
                port, "POST", f"/api/thread/{thread_id}/lock", cookie=jars["bob"].header()
            )[0]
            == 200
        )
        replies = _request_json(port, "GET", "/api/reply")[1]["reply"]
        reply_id = int(replies[0]["id"])
        assert (
            _request_json(
                port,
                "DELETE",
                f"/api/reply/{reply_id}",
                cookie=jars["alice"].header(),
            )[0]
            == 409
        )

        worker.kill()
        worker.boot(port=port)
        after_status, after_body = _request_json(port, "GET", "/api/thread")
        assert after_status == 200
        assert after_body["thread"][0]["title"] == "Moderated"
        assert after_body["thread"][0]["locked"] == 1


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request_json(
    port: int,
    method: str,
    path: str,
    *,
    body: dict[str, object] | None = None,
    cookie: str | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object]]:
    request_headers = dict(headers or {})
    data = None
    if body is not None:
        request_headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    if cookie is not None:
        request_headers["Cookie"] = cookie
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers=request_headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=15.0) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))
