"""Fail-closed container and volume cleanup."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable
from typing import Any

from disco.tools.sandbox import LocalSandboxService

from .constants import LEAK_CHECK_TIMEOUT_S
from .docker_cli import DockerCLI
from .util import clipped


async def await_with_deadline(
    awaitable: Awaitable[Any], *, deadline: float, max_timeout_s: float
) -> Any:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        if hasattr(awaitable, "close"):
            awaitable.close()  # type: ignore[union-attr]
        raise TimeoutError("global deadline exhausted")
    return await asyncio.wait_for(awaitable, timeout=min(max_timeout_s, remaining))


async def stop_tasks(
    tasks: list[asyncio.Task[Any]], *, deadline: float, max_timeout_s: float = 3
) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    remaining = deadline - time.monotonic()
    if tasks and remaining > 0:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                min(max_timeout_s, remaining),
            )


def _cleanup_result(
    instances: list[Any],
    empty_stability_s: float,
) -> dict[str, Any]:
    return {
        "instance_destroy_attempts": len(instances),
        "instance_destroy_failures": [],
        "destroy_by_conversation_attempted": False,
        "destroy_by_conversation_error": None,
        "leak_check_error": None,
        "leaks_before_forced_cleanup": [],
        "forced_cleanup": None,
        "forced_cleanup_attempts": [],
        "leak_sweeps": [],
        "required_empty_stability_s": round(empty_stability_s, 3),
        "leaks_remaining": [],
        "volume_leak_check_error": None,
        "volumes_before_forced_cleanup": [],
        "volume_cleanup_attempts": [],
        "volumes_remaining": [],
    }


async def _destroy_sdk_resources(
    service: LocalSandboxService | None,
    instances: list[Any],
    conversation_id: str,
    global_deadline: float,
    result: dict[str, Any],
) -> None:
    async def destroy_one(instance: Any) -> None:
        try:
            await await_with_deadline(
                instance.destroy(), deadline=global_deadline, max_timeout_s=20
            )
        except Exception as exc:  # noqa: BLE001 - continue with label cleanup
            result["instance_destroy_failures"].append(
                {
                    "instance_id": getattr(instance, "id", "unknown"),
                    "error": clipped(f"{type(exc).__name__}: {exc}"),
                }
            )

    if instances:
        await asyncio.gather(*(destroy_one(instance) for instance in instances))

    if service is not None:
        result["destroy_by_conversation_attempted"] = True
        try:
            await await_with_deadline(
                service.destroy_by_conversation(conversation_id),
                deadline=global_deadline,
                max_timeout_s=20,
            )
        except Exception as exc:  # noqa: BLE001 - direct CLI cleanup follows
            result["destroy_by_conversation_error"] = clipped(f"{type(exc).__name__}: {exc}")


async def _sleep_with_deadline(deadline: float, duration_s: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining > 0:
        await asyncio.sleep(min(duration_s, remaining))


async def _sweep_containers(
    docker_cli: DockerCLI,
    conversation_id: str,
    global_deadline: float,
    empty_stability_s: float,
    result: dict[str, Any],
) -> None:
    # Repeated ps -> rm -> ps closes two failure-path holes: a container that
    # appears just after an earlier snapshot, and a transient failed ps followed
    # by a successful authoritative check.  Create workers are drained before
    # entering cleanup; the repeated sweep is the final belt-and-suspenders.
    empty_since: float | None = None
    consecutive_check_errors = 0
    for sweep_index in range(1_000):
        remaining = global_deadline - time.monotonic()
        if remaining <= 0:
            result["leak_check_error"] = (
                result["leak_check_error"]
                or "global deadline exhausted before an empty docker ps label check"
            )
            break
        leaks, error = await docker_cli.labeled_containers(
            conversation_id, timeout_s=min(LEAK_CHECK_TIMEOUT_S, remaining)
        )
        result["leak_sweeps"].append(
            {"sweep": sweep_index + 1, "error": error, "containers": leaks}
        )
        if error:
            empty_since = None
            consecutive_check_errors += 1
            result["leak_check_error"] = error
            if consecutive_check_errors >= 3:
                break
            await _sleep_with_deadline(global_deadline, 0.10)
            continue

        # A successful check supersedes a prior transient check error.
        consecutive_check_errors = 0
        result["leak_check_error"] = None
        result["leaks_remaining"] = leaks
        if not leaks:
            now = time.monotonic()
            if empty_since is None:
                empty_since = now
            if now - empty_since >= empty_stability_s:
                break
            await _sleep_with_deadline(
                global_deadline,
                min(0.10, empty_stability_s - (now - empty_since)),
            )
            continue
        empty_since = None
        if not result["leaks_before_forced_cleanup"]:
            result["leaks_before_forced_cleanup"] = leaks

        remaining = global_deadline - time.monotonic()
        if remaining <= 0:
            result["leak_check_error"] = "global deadline exhausted before forced cleanup"
            break
        removal = await docker_cli.force_remove(
            [entry["id"] for entry in leaks], timeout_s=min(15, remaining)
        )
        if removal is None:
            continue
        removal_record = {
            "returncode": removal.returncode,
            "timed_out": removal.timed_out,
            "spawn_error": removal.spawn_error,
            "stdout": clipped(removal.stdout.strip()),
            "stderr": clipped(removal.stderr.strip()),
        }
        result["forced_cleanup"] = removal_record
        result["forced_cleanup_attempts"].append(removal_record)
    else:
        if result["leak_check_error"] is None:
            result["leak_check_error"] = "no stable empty result after 1000 label sweeps"


async def _sweep_volumes(
    docker_cli: DockerCLI,
    conversation_id: str,
    global_deadline: float,
    result: dict[str, Any],
) -> None:
    # LocalSandboxService uses labeled named volumes. The public service cleanup
    # normally removes them; this independent pass also covers a supervisor kill
    # between volume creation and construction of a trackable instance.
    for _attempt in range(3):
        remaining = global_deadline - time.monotonic()
        if remaining <= 0:
            result["volume_leak_check_error"] = (
                "global deadline exhausted before docker volume label check"
            )
            break
        volumes, error = await docker_cli.labeled_volumes(
            conversation_id, timeout_s=min(LEAK_CHECK_TIMEOUT_S, remaining)
        )
        result["volume_leak_check_error"] = error
        result["volumes_remaining"] = volumes
        if error:
            continue
        if not volumes:
            break
        if not result["volumes_before_forced_cleanup"]:
            result["volumes_before_forced_cleanup"] = volumes
        remaining = global_deadline - time.monotonic()
        if remaining <= 0:
            result["volume_leak_check_error"] = (
                "global deadline exhausted before forced volume cleanup"
            )
            break
        removal = await docker_cli.force_remove_volumes(
            [entry["name"] for entry in volumes], timeout_s=min(15, remaining)
        )
        if removal is not None:
            result["volume_cleanup_attempts"].append(
                {
                    "returncode": removal.returncode,
                    "timed_out": removal.timed_out,
                    "spawn_error": removal.spawn_error,
                    "stdout": clipped(removal.stdout.strip()),
                    "stderr": clipped(removal.stderr.strip()),
                }
            )


async def cleanup(
    *,
    service: LocalSandboxService | None,
    instances: list[Any],
    conversation_id: str,
    docker_cli: DockerCLI,
    global_deadline: float,
    empty_stability_s: float = 0.15,
) -> dict[str, Any]:
    """Destroy owned SDK resources, then prove container and volume absence."""
    result = _cleanup_result(instances, empty_stability_s)
    await _destroy_sdk_resources(
        service,
        instances,
        conversation_id,
        global_deadline,
        result,
    )
    await _sweep_containers(
        docker_cli,
        conversation_id,
        global_deadline,
        empty_stability_s,
        result,
    )
    await _sweep_volumes(
        docker_cli,
        conversation_id,
        global_deadline,
        result,
    )
    return result
