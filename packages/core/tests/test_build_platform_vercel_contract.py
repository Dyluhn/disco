"""Documented local contract for a future Vercel prebuilt connector.

This is deliberately a fixture-level contract test.  Epic 15 does not install
a Vercel connector or a Next.js target yet; it proves that a later connector
can use the existing target-neutral deployment seam without receiving source,
build authority, or secrets.
"""

from __future__ import annotations

from typing import ClassVar

from disco.core.build_platform import (
    ComponentId,
    ComponentIntent,
    DeploymentConnector,
    DeploymentPlan,
    DeploymentRequest,
    Parameter,
)


def _id(namespace: str, name: str) -> ComponentId:
    return ComponentId(namespace=namespace, name=name, version="1")


class _DocumentedPrebuiltConnector:
    """A contract fixture, not a shipped provider implementation."""

    id: ClassVar[ComponentId] = _id("vercel", "prebuilt-fixture")

    def plan(self, request: DeploymentRequest) -> DeploymentPlan:
        return DeploymentPlan(
            connector=self.id,
            environment=request.environment,
            intents=(
                ComponentIntent(
                    operation="deployment.upload_prebuilt",
                    parameters=(
                        Parameter(
                            name="artifact_digest_ref",
                            value=request.package_digest_ref,
                        ),
                        Parameter(
                            name="build_environment",
                            value="resolved_before_local_build",
                        ),
                        Parameter(name="runtime_environment", value="deployment_runtime"),
                        Parameter(name="provider_rebuild", value="unobserved"),
                    ),
                ),
            ),
        )


def test_documented_prebuilt_contract_preserves_artifact_and_scopes() -> None:
    connector = _DocumentedPrebuiltConnector()
    assert isinstance(connector, DeploymentConnector)

    digest = "artifact:sha256:" + "a" * 64
    request = DeploymentRequest(
        profile=_id("disco", "nextjs-profile"),
        target=_id("disco", "nextjs-target"),
        package_digest_ref=digest,
        environment="production",
    )
    plan = connector.plan(request)

    assert plan.connector == connector.id
    assert plan.environment == "production"
    assert plan.confirmation_required is True
    intent = plan.intents[0]
    parameters = {parameter.name: parameter.value for parameter in intent.parameters}
    assert intent.operation == "deployment.upload_prebuilt"
    assert parameters["artifact_digest_ref"] == digest
    assert parameters["build_environment"] == "resolved_before_local_build"
    assert parameters["runtime_environment"] == "deployment_runtime"
    assert parameters["provider_rebuild"] == "unobserved"


def test_deployment_request_has_no_source_or_secret_channel() -> None:
    assert set(DeploymentRequest.model_fields) == {
        "profile",
        "target",
        "package_digest_ref",
        "environment",
    }
    assert set(DeploymentRequest.model_fields).isdisjoint(
        {"source", "source_revision", "build_command", "secret", "token"}
    )
