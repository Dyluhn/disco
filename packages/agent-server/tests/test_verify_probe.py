from __future__ import annotations

from typing import Any

from disco.agent_server.verify.probe import (
    AppProbeTarget,
    app_body_problem,
    app_probe_targets,
    collect_web_app_probe,
    compute_web_app_verdict,
    validate_app_deliverables,
)
from disco.agent_server.verify.host import HostWebAppVerifier
from disco.core.loop import HostVerificationDeliverable


def test_app_probe_targets_collects_url_and_preview_targets() -> None:
    deliverables: list[dict[str, Any]] = [
        {
            "kind": "app",
            "path": "site",
            "deployment_url": "http://127.0.0.1:3000/",
        },
        {"kind": "app", "path": "nested/site/", "deployment_url": ""},
        {"kind": "file", "path": "deck.pptx"},
    ]

    assert app_probe_targets(deliverables) == [
        AppProbeTarget(
            kind="deployment_url",
            label="http://127.0.0.1:3000/",
            url="http://127.0.0.1:3000/",
        ),
        AppProbeTarget(kind="preview", label="nested/site"),
    ]


def test_app_body_problem_preserves_existing_response_rules() -> None:
    assert app_body_problem(None, label="x") == "app deliverable not reachable: x"
    assert (
        app_body_problem((503, b""), label="x")
        == "app deliverable preview not available (503): x"
    )
    assert (
        app_body_problem((300, b"<html>choices</html>"), label="x")
        == "app deliverable returned HTTP 300: x"
    )
    assert app_body_problem((200, b""), label="x") == "app deliverable is empty: x"
    assert (
        app_body_problem((200, b"<title>Directory listing for /</title>"), label="x")
        == "app deliverable is a bare directory listing, not a real app: x"
    )
    assert app_body_problem((200, b"<h1>Real app</h1>"), label="x") is None


class _ProbeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def fetch_app(self, url: str) -> tuple[int, bytes] | None:
        self.calls.append(("fetch_app", url))
        return (200, b"<h1>deployment</h1>")

    async def fetch_preview(self, cid: str) -> tuple[int, bytes] | None:
        self.calls.append(("fetch_preview", cid))
        return (503, b"")


async def test_validate_app_deliverables_fetches_collected_targets() -> None:
    client = _ProbeClient()
    problems = await validate_app_deliverables(
        [
            {
                "kind": "app",
                "path": "site",
                "deployment_url": "http://127.0.0.1:3000/",
            },
            {"kind": "app", "path": "site", "deployment_url": ""},
        ],
        client=client,
        cid="conv_123",
    )

    assert client.calls == [
        ("fetch_app", "http://127.0.0.1:3000/"),
        ("fetch_preview", "conv_123"),
    ]
    assert problems == ["app deliverable preview not available (503): site"]


async def test_host_web_app_verifier_client_fallback_uses_shared_verdict() -> None:
    client = _ProbeClient()
    verifier = HostWebAppVerifier(client=client)

    verdict = await verifier.verify(
        HostVerificationDeliverable(
            conversation_id="conv_123",
            artifact_path="site",
            artifact_kind="app",
        )
    )

    assert client.calls == [("fetch_preview", "conv_123")]
    assert verdict["passed"] is False
    assert verdict["verdict"] == "fail"
    assert "preview not available" in verdict["summary"]


def test_collect_web_app_probe_extracts_structured_diagnostics() -> None:
    probe = collect_web_app_probe(
        {
            "title": "App",
            "text": "  visible text  ",
            "elements": ["button"],
            "screenshot_path": ".pmx/screenshots/1.png",
            "console": [
                {
                    "level": "error",
                    "text": "ReferenceError: x is not defined",
                    "location": {
                        "url": "http://127.0.0.1:3000/app.js",
                        "lineNumber": 12,
                        "columnNumber": 3,
                    },
                },
                {"level": "warning", "text": "minor"},
            ],
            "network": [
                {
                    "method": "GET",
                    "url": "http://127.0.0.1:3000/favicon.ico",
                    "status": 404,
                },
                {"method": "POST", "url": "http://127.0.0.1:3000/api", "status": 500},
            ],
        }
    )

    assert probe["visible_text_chars"] == len("visible text")
    assert probe["elements_count"] == 1
    assert probe["console_errors"] == [
        {
            "text": "ReferenceError: x is not defined",
            "source": "http://127.0.0.1:3000/app.js:12:3",
            "stack": "",
        }
    ]
    assert probe["console_warnings"] == [{"text": "minor", "source": ""}]
    assert probe["network_failures"] == [
        {
            "method": "POST",
            "url": "http://127.0.0.1:3000/api",
            "status": 500,
            "failure": None,
        }
    ]
    assert probe["screenshot_path"] == ".pmx/screenshots/1.png"
    assert probe["failure_fingerprint"]


def test_compute_web_app_verdict_reuses_collected_probe_shape() -> None:
    verdict = compute_web_app_verdict(
        url="http://127.0.0.1:3000/",
        reachable=True,
        http_status=200,
        structured={
            "title": "App",
            "text": "Welcome",
            "elements": ["button"],
            "console": [{"level": "error", "text": "boom"}],
            "network": [],
        },
        meaningful=True,
    )

    assert verdict["passed"] is False
    assert verdict["verdict"] == "fail"
    assert verdict["console_errors"][0]["text"] == "boom"
    assert "console error" in verdict["summary"]
