"""Classifier-boundary instrumentation for gVisor stress."""

from __future__ import annotations

import asyncio
from typing import Any

from disco.tools.sandbox import (
    LocalSandboxService,
    SandboxError,
    SandboxUnavailableError,
)

from .constants import DEATH_WORDS
from .docker_cli import DockerCLI
from .types import Metrics
from .util import _LOG, clipped, utc_now


class ClassifierObserver:
    """Count exact final package verdicts without changing their behavior."""

    def __init__(self, metrics: Metrics, docker_cli: DockerCLI) -> None:
        self.metrics = metrics
        self.docker_cli = docker_cli

    async def observe(self, instance: Any, raw_exc: Exception, verdict: SandboxError) -> None:
        verdict_text = str(verdict)
        if isinstance(verdict, SandboxError) and not isinstance(verdict, SandboxUnavailableError):
            # Every raw backend throw that ends here is classified as a per-op
            # error rather than container death.  Keep that broad count, while
            # transient_downgrades is the narrower re-verify-window branch that
            # proves the shared hardening specifically overturned a death candidate.
            self.metrics.per_op_classifications += 1
            if "transient sandbox api error" in verdict_text.lower():
                self.metrics.transient_downgrades += 1
                _LOG.info(
                    "harness observed transient downgrade instance=%s verdict=%s",
                    instance.id,
                    verdict_text,
                )
            return

        if not (isinstance(verdict, SandboxUnavailableError) and DEATH_WORDS.search(verdict_text)):
            return

        # This counter is incremented only at the real final classifier boundary.
        # The independent inspect happens before this hook returns the verdict to
        # exec_shell's caller, so no caller can destroy/recreate the box first.
        self.metrics.death_verdicts += 1
        docker_id = str(getattr(getattr(instance, "_container", None), "id", "") or "")
        event: dict[str, Any] = {
            "at": utc_now(),
            "elapsed_s": round(self.metrics.since_start(), 6),
            "instance_id": instance.id,
            "docker_id": docker_id,
            "raw_exception": clipped(f"{type(raw_exc).__name__}: {raw_exc}"),
            "verdict": clipped(f"{type(verdict).__name__}: {verdict}"),
        }
        if not docker_id:
            evidence = {
                "running": None,
                "parse_error": "instance had no docker container id",
            }
        else:
            evidence = await self.docker_cli.inspect_state(docker_id)
        event["direct_inspect"] = evidence

        if evidence.get("running") is False:
            self.metrics.confirmed_deaths += 1
            outcome = "confirmed_dead"
        elif evidence.get("running") is True:
            self.metrics.confirmation_alive += 1
            outcome = "alive_needless_verdict"
        else:
            # An inspect 404/error is not proof of death.  This conservative
            # treatment is intentional: the bug under test is a transient 404.
            self.metrics.confirmation_inconclusive += 1
            outcome = "inconclusive_not_confirmed"
        event["outcome"] = outcome
        self.metrics.add_sample(self.metrics.death_confirmations, event)
        _LOG.warning(
            "harness death confirmation instance=%s docker_id=%s outcome=%s evidence=%s",
            instance.id,
            docker_id,
            outcome,
            evidence,
        )


def install_classifier_observer(service: LocalSandboxService, observer: ClassifierObserver) -> str:
    """Give newly-created instances a harness-only observer subclass."""
    base_cls = service._instance_cls  # type: ignore[attr-defined]  # harness instrumentation

    class ObservedInstance(base_cls):  # type: ignore[valid-type, misc]
        async def _classify_failure_async(self, exc: Exception) -> SandboxError:
            verdict = await super()._classify_failure_async(exc)
            try:
                await observer.observe(self, exc, verdict)
            except asyncio.CancelledError:
                raise
            except Exception as observer_exc:  # noqa: BLE001 - evidence must not alter verdict
                observer.metrics.instrumentation_errors += 1
                _LOG.exception(
                    "classifier observer failed for instance=%s: %s",
                    getattr(self, "id", "unknown"),
                    observer_exc,
                )
            return verdict

    ObservedInstance.__name__ = f"Observed{base_cls.__name__}"
    ObservedInstance.__qualname__ = ObservedInstance.__name__
    service._instance_cls = ObservedInstance  # type: ignore[attr-defined]
    return f"{base_cls.__module__}.{base_cls.__name__}"
