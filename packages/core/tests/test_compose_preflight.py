"""The Ubuntu 24.04 Compose-v1 failure must fail before container startup."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "version,config_result,expected",
    [
        ("1.29.2", 0, 2),
        ("2.39.4", 0, 0),
        ("5.3.1", 0, 0),
        ("2.39.4", 7, 7),
    ],
)
def test_explicit_provider_and_schema_are_checked_before_up(
    tmp_path, version, config_result, expected
):
    provider = tmp_path / "docker-compose"
    provider.write_text(f"#!/bin/sh\necho {version}\n")
    provider.chmod(0o755)
    calls = tmp_path / "calls"
    podman = tmp_path / "podman"
    podman.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n'
        'case "$*" in *"config --quiet"*) exit "$CONFIG_RESULT";; esac\n'
    )
    podman.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "PODMAN_COMPOSE_PROVIDER": str(provider),
        "CALLS": str(calls),
        "CONFIG_RESULT": str(config_result),
    }
    result = subprocess.run(
        ["bash", str(ROOT / "deploy/compose/disco-compose"), "up", "-d"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == expected
    commands = calls.read_text().splitlines() if calls.exists() else []
    if version.startswith("1"):
        assert not commands
        assert "docker-compose-v2" in result.stderr
    elif config_result:
        assert len(commands) == 1 and "config --quiet" in commands[0]
    else:
        assert len(commands) == 2
        assert commands[0].endswith("config --quiet")
        assert commands[1].endswith("up -d")


def test_modern_plugin_is_selected_even_when_v1_is_on_path(tmp_path):
    plugins = tmp_path / "docker" / "cli-plugins"
    plugins.mkdir(parents=True)
    modern = plugins / "docker-compose"
    modern.write_text("#!/bin/sh\necho 2.39.4\n")
    modern.chmod(0o755)
    legacy = tmp_path / "docker-compose"
    legacy.write_text("#!/bin/sh\necho 1.29.2\n")
    legacy.chmod(0o755)
    podman = tmp_path / "podman"
    podman.write_text('#!/bin/sh\nprintf "%s" "$PODMAN_COMPOSE_PROVIDER"\n')
    podman.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "DOCKER_CONFIG": str(plugins.parent),
    }
    env.pop("PODMAN_COMPOSE_PROVIDER", None)
    result = subprocess.run(
        ["bash", str(ROOT / "deploy/compose/disco-compose"), "version"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout == str(modern)
