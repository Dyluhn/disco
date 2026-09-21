"""Paste-a-config MCP import — parser table + service round-trip + endpoint.

The parser must accept the JSON shapes real MCP server READMEs ship (wrapped,
bare, single-server, fenced, trailing-comma) and refuse garbage with an error
naming what is missing. The service must mirror the parse server-side, store
pasted literal secrets ONLY in the SecretStore (never in config, never echoed),
and create servers through the same validated path as the manual form.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from disco.app_server import create_app
from disco.app_server.config_state import ConfigState
from disco.app_server.mcp_import import McpImportError, parse_mcp_import
from disco.core import SkillStore, SqliteEventStore
from disco.core.llm import ConfigStore, SecretBox, SecretStore
from disco.tools.mcp.config import McpServerConfig
from fastapi.testclient import TestClient

# ---- parser table -----------------------------------------------------------

FILESYSTEM_README = """
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    }
  }
}
"""

GITHUB_ENV_LITERAL = json.dumps(
    {
        "mcpServers": {
            "github": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-github"],
                "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_secret123"},
            }
        }
    }
)

URL_WITH_HEADERS = json.dumps(
    {
        "mcpServers": {
            "linear": {
                "url": "https://mcp.linear.app/mcp",
                "headers": {"Authorization": "Bearer abc123"},
            }
        }
    }
)


def test_parses_classic_stdio_readme_blob() -> None:
    (row,) = parse_mcp_import(FILESYSTEM_README)
    assert row.get("error") is None
    assert row["name"] == "filesystem"
    assert row["transport"] == "stdio"
    assert row["url"] == "npx"
    assert row["args"] == ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]


def test_literal_env_value_becomes_pending_secret_not_config() -> None:
    (row,) = parse_mcp_import(GITHUB_ENV_LITERAL)
    ref = row["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"]
    assert ref == "mcp_github_github_personal_access_token"
    assert row["secrets"] == [
        {"ref": ref, "source_key": "GITHUB_PERSONAL_ACCESS_TOKEN", "value": "ghp_secret123"}
    ]


def test_placeholder_env_value_references_existing_secret() -> None:
    blob = json.dumps(
        {"mcpServers": {"brave": {"command": "npx brave", "env": {"KEY": "${BRAVE_API_KEY}"}}}}
    )
    (row,) = parse_mcp_import(blob)
    assert row["env"] == {"KEY": "BRAVE_API_KEY"}
    assert row["secrets"] == []  # nothing to store — the ref names a user secret


def test_url_variant_with_headers() -> None:
    (row,) = parse_mcp_import(URL_WITH_HEADERS)
    assert row["transport"] == "streamable_http"
    assert row["url"] == "https://mcp.linear.app/mcp"
    ref = row["headers"]["Authorization"]
    assert ref == "mcp_linear_authorization"
    assert row["secrets"][0]["value"] == "Bearer abc123"


def test_bare_inner_map_without_wrapper_key() -> None:
    blob = json.dumps({"memory": {"command": "npx -y @modelcontextprotocol/server-memory"}})
    (row,) = parse_mcp_import(blob)
    assert row["name"] == "memory"
    assert row["transport"] == "stdio"


def test_single_server_without_name_derives_one() -> None:
    blob = json.dumps({"command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"]})
    (row,) = parse_mcp_import(blob)
    assert row["name"] == "server_memory"
    assert any("no server name" in w for w in row["warnings"])


def test_vscode_servers_key_and_http_type_alias() -> None:
    blob = json.dumps({"servers": {"fetch": {"type": "http", "url": "https://example.com/mcp"}}})
    (row,) = parse_mcp_import(blob)
    assert row["transport"] == "streamable_http"


def test_markdown_fences_and_trailing_commas_tolerated() -> None:
    blob = (
        '```json\n{\n "mcpServers": {\n  "fs": {\n   "command": "npx fs-server",\n  },\n },\n}\n```'
    )
    (row,) = parse_mcp_import(blob)
    assert row["name"] == "fs"
    assert row.get("error") is None


def test_sse_transport_is_a_named_per_server_error() -> None:
    blob = json.dumps({"mcpServers": {"old": {"type": "sse", "url": "https://x.example/sse"}}})
    (row,) = parse_mcp_import(blob)
    assert "SSE" in row["error"]


def test_garbage_raises_with_json_position() -> None:
    with pytest.raises(McpImportError, match="not valid JSON"):
        parse_mcp_import("this is not json")


def test_empty_object_names_the_expected_shapes() -> None:
    with pytest.raises(McpImportError, match="mcpServers"):
        parse_mcp_import("{}")


def test_server_missing_command_and_url_is_specific() -> None:
    blob = json.dumps({"mcpServers": {"mystery": {"enabled": True}}})
    (row,) = parse_mcp_import(blob)
    assert '"command"' in row["error"] and '"url"' in row["error"]


def test_name_normalization_warns() -> None:
    blob = json.dumps({"mcpServers": {"Brave-Search": {"command": "npx brave"}}})
    (row,) = parse_mcp_import(blob)
    assert row["name"] == "brave_search"
    assert any("normalized" in w for w in row["warnings"])


def test_command_as_list_merges_with_args() -> None:
    blob = json.dumps(
        {"mcpServers": {"local": {"command": ["node", "server.js"], "args": ["--port", "3"]}}}
    )
    (row,) = parse_mcp_import(blob)
    assert row["url"] == "node"
    assert row["args"] == ["server.js", "--port", "3"]


def test_cwd_and_disabled_and_misplaced_env_warn() -> None:
    blob = json.dumps(
        {
            "mcpServers": {
                "svc": {
                    "url": "https://mcp.example.com",
                    "cwd": "/srv",
                    "disabled": True,
                    "env": {"K": "v"},
                }
            }
        }
    )
    (row,) = parse_mcp_import(blob)
    joined = " ".join(row["warnings"])
    assert "'cwd'" in joined and "disabled" in joined and '"env" only applies' in joined


def test_multiple_servers_parse_independently() -> None:
    blob = json.dumps(
        {
            "mcpServers": {
                "good": {"command": "npx ok"},
                "bad": {"type": "sse", "url": "https://x/sse"},
            }
        }
    )
    rows = parse_mcp_import(blob)
    assert [r.get("error") is None for r in rows] == [True, False]


# ---- service round-trip + endpoint ------------------------------------------


@pytest.fixture
def config_store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(tmp_path / "config.json")


@pytest.fixture
def config_state(tmp_path: Path, config_store: ConfigStore) -> ConfigState:
    return ConfigState(
        store=config_store,
        secrets=SecretStore(
            tmp_path / "secrets.json",
            box=SecretBox("test-app-secret-with-at-least-thirty-two-bytes"),
        ),
        skills=SkillStore(tmp_path / "skills"),
        db_conn=None,
    )


@pytest.fixture
def client(config_state: ConfigState, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DISCO_AUTH_DEV_AUTO_PAIR", "1")
    event_store = SqliteEventStore(":memory:")
    try:
        yield TestClient(create_app(event_store, config_state))
    finally:
        event_store.close()


def test_dry_run_previews_without_persisting(config_state: ConfigState) -> None:
    result = config_state._mcp_service.import_mcp_config(GITHUB_ENV_LITERAL, dry_run=True)
    assert result["ok"] is True and result["dry_run"] is True
    (row,) = result["servers"]
    assert row["created"] is False
    assert config_state._mcp_service._mcp_config().servers == {}
    assert not config_state._secrets.has_secret("mcp_github_github_personal_access_token")
    # A pasted secret VALUE must never be echoed in the response.
    assert "ghp_secret123" not in json.dumps(result)
    assert row["secrets"][0] == {
        "ref": "mcp_github_github_personal_access_token",
        "source_key": "GITHUB_PERSONAL_ACCESS_TOKEN",
        "value_provided": True,
        "already_configured": False,
    }


def test_apply_creates_server_and_stores_secret(config_state: ConfigState) -> None:
    result = config_state._mcp_service.import_mcp_config(GITHUB_ENV_LITERAL, dry_run=False)
    (row,) = result["servers"]
    assert row["created"] is True
    servers = config_state._mcp_service._mcp_config().servers
    srv = servers["github"]
    # Persisted config carries the REF, not the value — and revalidates as the
    # exact model the agent-server pool loads.
    assert srv["env"] == {"GITHUB_PERSONAL_ACCESS_TOKEN": "mcp_github_github_personal_access_token"}
    McpServerConfig.model_validate({"name": "github", **srv})
    assert (
        config_state._secrets.get_secret("mcp_github_github_personal_access_token")
        == "ghp_secret123"
    )
    assert "ghp_secret123" not in json.dumps(result)
    conn = next(c for c in config_state.mcp_connections() if c.id == "github")
    assert conn.status == "approval_required"


def test_apply_http_server_with_header_secret(config_state: ConfigState) -> None:
    result = config_state._mcp_service.import_mcp_config(URL_WITH_HEADERS, dry_run=False)
    (row,) = result["servers"]
    assert row["created"] is True
    srv = config_state._mcp_service._mcp_config().servers["linear"]
    assert srv["transport"] == "streamable_http"
    assert srv["headers"] == {"Authorization": "mcp_linear_authorization"}
    assert config_state._secrets.get_secret("mcp_linear_authorization") == "Bearer abc123"


def test_duplicate_name_is_a_per_server_error(config_state: ConfigState) -> None:
    config_state._mcp_service.import_mcp_config(FILESYSTEM_README, dry_run=False)
    result = config_state._mcp_service.import_mcp_config(FILESYSTEM_README, dry_run=False)
    (row,) = result["servers"]
    assert "already exists" in row["error"]
    assert result["ok"] is False


def test_import_endpoint_previews_and_creates(client: TestClient) -> None:
    preview = client.post("/api/mcp/servers/import", json={"text": FILESYSTEM_README})
    assert preview.status_code == 200
    assert preview.json()["dry_run"] is True

    applied = client.post(
        "/api/mcp/servers/import", json={"text": FILESYSTEM_README, "dry_run": False}
    )
    assert applied.status_code == 200
    assert applied.json()["servers"][0]["created"] is True

    listed = {c["id"] for c in client.get("/api/mcp").json()}
    assert "filesystem" in listed


def test_import_endpoint_rejects_garbage_with_400(client: TestClient) -> None:
    resp = client.post("/api/mcp/servers/import", json={"text": "not json at all"})
    assert resp.status_code == 400
    assert "not valid JSON" in resp.json()["detail"]


def test_create_endpoint_accepts_env_and_headers(client: TestClient) -> None:
    made = client.post(
        "/api/mcp/servers",
        json={
            "name": "withenv",
            "url": "npx -y some-server",
            "transport": "stdio",
            "env": {"API_KEY": "my_secret_ref"},
        },
    )
    assert made.status_code == 201
    http_made = client.post(
        "/api/mcp/servers",
        json={
            "name": "withheaders",
            "url": "https://mcp.example.com/mcp",
            "transport": "streamable_http",
            "headers": {"Authorization": "my_header_ref"},
        },
    )
    assert http_made.status_code == 201


def test_patch_transport_flip_drops_stale_launch_and_header_fields(
    config_state: ConfigState, client: TestClient
) -> None:
    client.post(
        "/api/mcp/servers",
        json={
            "name": "flipper",
            "url": "https://mcp.example.com/mcp",
            "transport": "streamable_http",
            "headers": {"Authorization": "ref_a"},
        },
    )
    flipped = client.patch(
        "/api/mcp/servers/flipper",
        json={"transport": "stdio", "url": "npx -y some-server"},
    )
    assert flipped.status_code == 200
    srv = config_state._mcp_service._mcp_config().servers["flipper"]
    assert "headers" not in srv
    assert srv["command"] == ["npx"]
