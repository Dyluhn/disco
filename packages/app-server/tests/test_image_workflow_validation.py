from __future__ import annotations

import pytest
from disco.app_server import create_app
from disco.app_server.config_state import ConfigState
from disco.core import SkillStore, SqliteEventStore
from disco.core.llm import ConfigStore, SecretBox, SecretStore
from fastapi.testclient import TestClient


@pytest.mark.parametrize("invalid", ["{invalid", "[]", "{}", "null", '{"seed":"%seed%"}'])
def test_invalid_workflow_cannot_replace_saved_configuration(tmp_path, invalid):
    state = ConfigState(
        store=ConfigStore(tmp_path / "config.json"),
        secrets=SecretStore(tmp_path / "secrets.json", box=SecretBox("test-app-secret")),
        skills=SkillStore(tmp_path / "skills"),
    )
    with TestClient(create_app(SqliteEventStore(":memory:"), state)) as client:
        url = "/api/image-gen/config"
        valid = {"provider": "comfyui", "base_url": "http://localhost:8188", "model": "custom",
                 "workflow_json": '{"1":{"inputs":{"seed":%seed%,"text":"%prompt%"}}}'}
        assert client.put(url, json=valid).status_code == 200
        before = client.get(url).json()
        response = client.put(url, json={**valid, "workflow_json": invalid})
        assert response.status_code == 400
        assert response.json()["detail"]["reason"] == "invalid_image_workflow"
        assert client.get(url).json() == before
        assert client.put(url, json={**valid, "workflow_json": ""}).status_code == 200


def test_invalid_inactive_workflow_does_not_block_switching_provider(tmp_path):
    state = ConfigState(
        store=ConfigStore(tmp_path / "config.json"),
        secrets=SecretStore(tmp_path / "secrets.json", box=SecretBox("test-app-secret")),
        skills=SkillStore(tmp_path / "skills"),
    )
    with TestClient(create_app(SqliteEventStore(":memory:"), state)) as client:
        url = "/api/image-gen/config"
        switched = {"provider": "openrouter", "base_url": "", "model": "",
                    "workflow_json": "{invalid"}
        assert client.put(url, json=switched).status_code == 200
        assert client.get(url).json()["provider"] == "openrouter"
        comfy = {**switched, "provider": "comfyui", "base_url": "http://localhost:8188"}
        response = client.put(url, json=comfy)
        assert response.status_code == 400
        assert response.json()["detail"]["reason"] == "invalid_image_workflow"
