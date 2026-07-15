from __future__ import annotations

import hashlib
import json
import socket
import sqlite3
import uuid

import pytest
from _workerd_harness import WorkerdApp, wrangler_available
from disco.core.appkit import (
    RECORDS_PRIMITIVE_ID,
    default_lead_gen_app_spec,
    default_records_app_spec,
    generate,
    get_recipe,
    resolve_primitive,
)
from disco.core.appkit.spec import AppSpec, Entity, EntityField

_LEAD_GEN_ACME_DIGEST = "89fd638c3e192926ea5cc27d3a4f8bd43ca25a8b0b69c924f7750a2e23f59d21"
_RECORDS_WORKER_SCHEMA_DIGEST = "88a20d2d82bcbbc5a32640d39b2496ad4ed013926096ca7b0f6b08622517af1d"


def _design():
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    return recipe.to_design_spec()


def _records_app() -> AppSpec:
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    return default_records_app_spec("Team Ops", recipe)


def _tree() -> dict[str, str]:
    return generate(_records_app(), _design())


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


def test_records_schema_has_fk_order_and_init_migration() -> None:
    tree = _tree()
    schema = tree["schema.sql"]
    assert 'CREATE TABLE IF NOT EXISTS "team_member"' in schema
    assert 'CREATE TABLE IF NOT EXISTS "shift"' in schema
    assert schema.index('"team_member"') < schema.index('"shift"')
    assert 'FOREIGN KEY("member_id") REFERENCES "team_member"("id")' in schema
    assert tree["migrations/0001_init.sql"] == schema


def test_records_drizzle_declares_referenced_table_first() -> None:
    schema_ts = _tree()["src/db/schema.ts"]
    assert schema_ts.index("export const team_member") < schema_ts.index("export const shift")
    assert ".references(() => team_member.id)" in schema_ts


def test_records_worker_has_per_entity_routes_and_imports() -> None:
    worker = _tree()["worker/index.ts"]
    assert 'import { team_member, shift } from "../src/db/schema";' in worker
    assert "POST /api/team_member" in worker
    assert "GET /api/shift" in worker
    assert "db.insert(shift)" in worker


def test_records_worker_and_schema_outputs_stay_stable() -> None:
    assert (
        _digest_paths(
            _tree(),
            ("schema.sql", "migrations/0001_init.sql", "worker/index.ts", "src/db/schema.ts"),
        )
        == _RECORDS_WORKER_SCHEMA_DIGEST
    )


def test_records_schema_sql_round_trips_with_fk_enforcement() -> None:
    con = sqlite3.connect(":memory:")
    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.executescript(_tree()["schema.sql"])
        con.execute(
            'INSERT INTO "team_member" ("name", "email") VALUES (?, ?)',
            ("Ada", "ada@example.com"),
        )
        con.execute(
            'INSERT INTO "shift" ("title", "starts_at", "member_id") VALUES (?, ?, ?)',
            ("Open", "2026-07-06T09:00:00Z", 1),
        )
        row = con.execute('SELECT "title", "member_id" FROM "shift" WHERE "id" = 1').fetchone()
        assert row == ("Open", 1)
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                'INSERT INTO "shift" ("title", "starts_at", "member_id") VALUES (?, ?, ?)',
                ("Bad", "2026-07-06T10:00:00Z", 999),
            )
    finally:
        con.close()


def test_records_app_spec_rejects_missing_fk_target_and_cycle() -> None:
    with pytest.raises(ValueError, match="references unknown entity"):
        AppSpec(
            schema_version=1,
            app_kind=RECORDS_PRIMITIVE_ID,
            name="Bad",
            entities=(
                Entity(
                    id="shift",
                    name="Shift",
                    fields=(
                        EntityField(
                            name="member_id",
                            type="int",
                            required=True,
                            references="missing",
                        ),
                    ),
                ),
            ),
        )

    with pytest.raises(ValueError, match="foreign key cycle"):
        AppSpec(
            schema_version=1,
            app_kind=RECORDS_PRIMITIVE_ID,
            name="Cycle",
            entities=(
                Entity(
                    id="a",
                    name="A",
                    fields=(EntityField(name="b_id", type="int", references="b"),),
                ),
                Entity(
                    id="b",
                    name="B",
                    fields=(EntityField(name="a_id", type="int", references="a"),),
                ),
            ),
        )


def test_records_primitive_is_registered_and_lead_gen_stays_byte_identical() -> None:
    assert resolve_primitive(RECORDS_PRIMITIVE_ID).id == RECORDS_PRIMITIVE_ID
    recipe = get_recipe("editorial-ledger")
    assert recipe is not None
    tree = generate(default_lead_gen_app_spec("Acme", recipe), recipe.to_design_spec())
    assert _digest(tree) == _LEAD_GEN_ACME_DIGEST


@pytest.mark.integration
def test_records_fk_survives_cold_restart() -> None:
    if not wrangler_available():
        pytest.skip("wrangler is required for the local workerd records proof")

    tree = generate(_records_app(), _design())
    marker = f"cold restart shift {uuid.uuid4().hex}"
    port = _free_port()

    with WorkerdApp(tree, admin_token="recordtoken123") as w:
        w.boot(port=port)

        member_status, member_body = w.post_json(
            "/api/team_member",
            {"name": "Ada Lovelace", "email": f"ada-{uuid.uuid4().hex}@example.com"},
        )
        print(f"POST /api/team_member -> {member_status} {member_body}")
        assert member_status == 201
        assert json.loads(member_body) == {"ok": True}

        shift_status, shift_body = w.post_json(
            "/api/shift",
            {
                "title": marker,
                "starts_at": "2026-07-06T09:00:00Z",
                "member_id": 1,
            },
        )
        print(f"POST /api/shift member_id=1 marker={marker!r} -> {shift_status} {shift_body}")
        assert shift_status == 201
        assert json.loads(shift_body) == {"ok": True}

        before_status, before_body = w.get("/api/shift", token="recordtoken123")
        before = json.loads(before_body)
        print(
            "before restart authed GET /api/shift "
            f"-> {before_status}; marker present={marker in before_body}"
        )
        assert before_status == 200
        assert _has_shift(before, marker)

        w.kill()
        assert w.is_down()
        print(f"killed wrangler dev on port {port}; port down={w.is_down()}")

        w.boot(port=port)

        after_status, after_body = w.get("/api/shift", token="recordtoken123")
        after = json.loads(after_body)
        print(
            "after restart authed GET /api/shift "
            f"-> {after_status}; marker present={marker in after_body}"
        )
        assert after_status == 200
        assert _has_shift(after, marker)


def _has_shift(payload: object, marker: str) -> bool:
    if not isinstance(payload, dict):
        return False
    rows = payload.get("shift")
    if not isinstance(rows, list):
        return False
    return any(
        isinstance(row, dict) and row.get("title") == marker and row.get("member_id") == 1
        for row in rows
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        assert isinstance(port, int)
        return port
