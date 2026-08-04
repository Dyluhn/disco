"""Target-owned Next.js deployment connectors.

This module owns the deployment connector vocabulary for the Next.js targets.
It is the target-specific layer that turns a target-neutral
:class:`DeploymentRequest` into a :class:`DeploymentPlan`.  It deliberately
carries no authenticated-provider, no source/build command, no
token/secret/credential, and no deployment effect.  The self-host connector
(15-C1) and the optional strict-prebuilt Vercel connector (15-C2) are distinct
connectors with distinct operations; identity parity (15-C3) remains a separate
boundary.
"""

from __future__ import annotations

from ..build_platform import (
    ComponentId,
    ComponentIntent,
    DeploymentConnector,
    DeploymentPlan,
    DeploymentRequest,
    Parameter,
)

NEXTJS_SELF_HOST_CONNECTOR_ID = ComponentId(
    namespace="disco", name="nextjs_self_host", version="1"
)
NEXTJS_VERCEL_PREBUILT_CONNECTOR_ID = ComponentId(
    namespace="disco", name="nextjs_vercel_prebuilt", version="1"
)

SELF_HOST_DEPLOY_OPERATION = "deployment.nextjs_self_host"
VERCEL_PREBUILT_DEPLOY_OPERATION = "deployment.nextjs_vercel_prebuilt"
DIGEST_PARAMETER_NAME = "package_digest_ref"

ARTIFACT_DIGEST_PARAMETER_NAME = "artifact_digest_ref"
ARTIFACT_MODE_PARAMETER_NAME = "artifact_mode"
BUILD_ENVIRONMENT_PARAMETER_NAME = "build_environment"
RUNTIME_ENVIRONMENT_PARAMETER_NAME = "runtime_environment"
PROVIDER_REBUILD_PARAMETER_NAME = "provider_rebuild"

ARTIFACT_MODE_STRICT_PREBUILT = "strict_prebuilt"
BUILD_ENVIRONMENT_RESOLVED_BEFORE_LOCAL_BUILD = "resolved_before_local_build"
RUNTIME_ENVIRONMENT_DEPLOYMENT_RUNTIME = "deployment_runtime"
PROVIDER_REBUILD_UNOBSERVED = "unobserved"


class NextjsSelfHostConnector(DeploymentConnector):
    """First-class self-host connector: intent and confirmation, no effect."""

    @property
    def id(self) -> ComponentId:
        return NEXTJS_SELF_HOST_CONNECTOR_ID

    def plan(self, request: DeploymentRequest) -> DeploymentPlan:
        """Resolve the self-host deployment plan with the exact request digest.

        The plan carries exactly one intent with exactly one parameter,
        ``package_digest_ref``, copied byte-for-byte from the request.  It
        exposes no source, build command, token, secret, or credential channel
        and executes no deployment effect.
        """
        return DeploymentPlan(
            connector=NEXTJS_SELF_HOST_CONNECTOR_ID,
            environment=request.environment,
            intents=(
                ComponentIntent(
                    operation=SELF_HOST_DEPLOY_OPERATION,
                    parameters=(
                        Parameter(
                            name=DIGEST_PARAMETER_NAME,
                            value=request.package_digest_ref,
                        ),
                    ),
                ),
            ),
            confirmation_required=True,
        )


class NextjsVercelPrebuiltConnector(DeploymentConnector):
    """Optional strict-prebuilt Vercel connector: intent and confirmation only.

    This connector plans an upload of an already-built artifact.  It copies the
    request's exact ``package_digest_ref`` byte-for-byte as
    ``artifact_digest_ref``, keeps build-time and runtime environment parameters
    distinct, and reports ``provider_rebuild`` as ``unobserved`` rather than
    claiming a provider success/failure.  It exposes no source, build command,
    token, secret, or credential channel and executes no deployment effect.
    """

    @property
    def id(self) -> ComponentId:
        return NEXTJS_VERCEL_PREBUILT_CONNECTOR_ID

    def plan(self, request: DeploymentRequest) -> DeploymentPlan:
        """Resolve the strict-prebuilt Vercel deployment plan.

        The plan carries exactly one intent with exactly five parameters:
        ``artifact_digest_ref`` (the request digest), ``artifact_mode``
        (``strict_prebuilt``), ``build_environment``
        (``resolved_before_local_build``), ``runtime_environment``
        (``deployment_runtime``), and ``provider_rebuild`` (``unobserved``).
        """
        return DeploymentPlan(
            connector=NEXTJS_VERCEL_PREBUILT_CONNECTOR_ID,
            environment=request.environment,
            intents=(
                ComponentIntent(
                    operation=VERCEL_PREBUILT_DEPLOY_OPERATION,
                    parameters=(
                        Parameter(
                            name=ARTIFACT_DIGEST_PARAMETER_NAME,
                            value=request.package_digest_ref,
                        ),
                        Parameter(
                            name=ARTIFACT_MODE_PARAMETER_NAME,
                            value=ARTIFACT_MODE_STRICT_PREBUILT,
                        ),
                        Parameter(
                            name=BUILD_ENVIRONMENT_PARAMETER_NAME,
                            value=BUILD_ENVIRONMENT_RESOLVED_BEFORE_LOCAL_BUILD,
                        ),
                        Parameter(
                            name=RUNTIME_ENVIRONMENT_PARAMETER_NAME,
                            value=RUNTIME_ENVIRONMENT_DEPLOYMENT_RUNTIME,
                        ),
                        Parameter(
                            name=PROVIDER_REBUILD_PARAMETER_NAME,
                            value=PROVIDER_REBUILD_UNOBSERVED,
                        ),
                    ),
                ),
            ),
            confirmation_required=True,
        )
