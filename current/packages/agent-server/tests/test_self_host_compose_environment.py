"""Truthful environment ownership for the repository self-host Compose stack."""

from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[4]


def _compose() -> dict:
    parsed = yaml.safe_load((_REPO / "compose.yaml").read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def test_agent_server_receives_every_advertised_runtime_override() -> None:
    environment = _compose()["services"]["agent-server"]["environment"]

    # One level of interpolation only — see
    # core/tests/test_compose_interpolation_portability.py for why the legacy
    # ${DISCO_X:-${PMX_X:-default}} form cannot survive a distribution's compose.
    assert environment["DISCO_BUILD_EGRESS"] == "${DISCO_BUILD_EGRESS:-filtered}"
    assert environment["DISCO_DRIVER_VISION"] == "${DISCO_DRIVER_VISION:-0}"
    assert environment["DISCO_EMBEDDER_URL"] == "${DISCO_EMBEDDER_URL:-}"
    assert environment["DISCO_RERANKER_URL"] == "${DISCO_RERANKER_URL:-}"
    assert environment["DISCO_NLI_URL"] == "${DISCO_NLI_URL:-}"


def test_logging_inspect_and_provider_variables_reach_their_consumers() -> None:
    services = _compose()["services"]
    app = services["app-server"]["environment"]
    agent_service = services["agent-server"]
    agent = agent_service["environment"]

    assert app["DISCO_AGENT_BASE"] == "http://agent-server:8000"
    assert agent["DISCO_NLI_MODEL_DIR"] == "/opt/disco-cache/nli"

    assert agent["DISCO_INSPECT"] == "${DISCO_INSPECT:-0}"
    assert app["DISCO_APPROVALS"] == "/data/disco-approved-origins.json"
    assert agent["DISCO_APPROVALS"] == "/data/disco-approved-origins.json"
    assert agent_service["security_opt"] == ["label=disable"]
    assert agent["DISCO_LOCAL_ENGINE"] == "${DISCO_LOCAL_ENGINE:-podman}"
    for name in ("DISCO_SECRET_KEY", "DISCO_SECRET_KEY_ID", "DISCO_SECRET_READ_KEYS"):
        assert name in app
        assert name in agent
    for name in ("DISCO_LOG_LEVEL", "DISCO_LOG_JSON"):
        assert name in app
        assert name in agent
    for name in (
        "DISCO_OPENROUTER_API_KEY",
        "DISCO_GEMMA_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "TAVILY_API_KEY",
        "BRAVE_API_KEY",
        "BRAVE_SEARCH_API_KEY",
        "FIRECRAWL_API_KEY",
        "OPENAI_IMAGE_API_KEY",
        "OPENAI_TTS_API_KEY",
    ):
        assert name in app
        assert name in agent


def test_example_and_self_host_doc_name_the_effective_overrides() -> None:
    example = (_REPO / ".env.example").read_text(encoding="utf-8")
    docs = (_REPO / "current" / "docs" / "self-host.md").read_text(encoding="utf-8")

    for name in (
        "DISCO_BUILD_EGRESS",
        "DISCO_DRIVER_VISION",
        "DISCO_EMBEDDER_URL",
        "DISCO_RERANKER_URL",
        "DISCO_NLI_URL",
        "DISCO_INSPECT",
        "DISCO_LOG_LEVEL",
        "DISCO_LOG_JSON",
        "DISCO_SECRET_KEY_ID",
        "DISCO_SECRET_READ_KEYS",
    ):
        assert name in example
        assert name in docs
    assert "rotate_secret_store.py" in docs


def test_server_image_ships_the_js_runtime_stdio_mcp_servers_need() -> None:
    """`disco.tools.mcp.stdio` spawns the MCP server subprocess inside THIS
    image (the agent-server process owns the stdio transport — it is not handed
    to a sandbox container), and published stdio servers are launched with
    `npx -y ...`. Without node/npx the launch fails with FileNotFoundError, which
    is what shipped. Pin the runtime's presence and its lean copy-from shape —
    no apt node-* fan-out, no build toolchain."""

    dockerfile = (_REPO / "current" / "deploy" / "compose" / "Dockerfile.server").read_text(
        encoding="utf-8"
    )

    assert "FROM docker.io/library/node:22-bookworm-slim AS nodejs" in dockerfile
    assert "COPY --from=nodejs /usr/local/bin/node /usr/local/bin/node" in dockerfile
    assert (
        "COPY --from=nodejs /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm"
        in dockerfile
    )
    assert "/usr/local/bin/npx" in dockerfile
    # The lean guarantee: the apt package list gains nothing — no distro node
    # fan-out, no build toolchain. Read the actual `apt-get install` package
    # list so the rationale comments above stay free text.
    apt_block = dockerfile.split("RUN apt-get update", 1)[1].split("rm -rf /var/lib/apt/lists", 1)[
        0
    ]
    apt_packages = set(apt_block.replace("\\", "").split())
    assert (
        apt_packages
        & {
            "nodejs",
            "npm",
            "node-gyp",
            "build-essential",
            "gcc",
            "python3-dev",
        }
        == set()
    )

    docs = (_REPO / "current" / "docs" / "self-host.md").read_text(encoding="utf-8")
    assert "stdio MCP" in docs
