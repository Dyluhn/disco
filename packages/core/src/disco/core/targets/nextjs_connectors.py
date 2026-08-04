"""Target-owned Next.js first-class self-host connector.

This module owns the self-host deployment connector vocabulary.  It is the
target-specific layer that turns a target-neutral
:class:`DeploymentRequest` into a self-host :class:`DeploymentPlan`.  It
deliberately carries no Vercel, no authenticated-provider, no source/build
command, no token/secret/credential, no artifact mode, and no
`provider_rebuild` behavior: 15-C2 (Vercel) and identity parity (15-C3)
remain separate boundaries.
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

SELF_HOST_DEPLOY_OPERATION = "deployment.nextjs_self_host"
DIGEST_PARAMETER_NAME = "package_digest_ref"


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
