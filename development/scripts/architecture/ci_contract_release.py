"""Release-workflow follower jobs: everything after the gated draft-release job.

Image publishing runs in its own jobs (one native runner per architecture), so
the release workflow is no longer a single job. The rule that keeps the gates
in front of publishing: every other job must reach the gated job through
``needs`` and must not be skippable.
"""

from __future__ import annotations

from typing import Any


def _needs(job: dict[str, Any]) -> list[str]:
    needs = job.get("needs")
    if isinstance(needs, str):
        return [needs]
    if isinstance(needs, list):
        return [item for item in needs if isinstance(item, str)]
    return []


def check_release_followers(jobs: dict[str, Any], release_job: str) -> list[str]:
    """Every job other than the gated one (image publishing, for one) must sit
    downstream of it through ``needs`` — directly or through another follower —
    and must not be skippable, so nothing publishes unless every gate passed."""
    problems: list[str] = []
    for name, follower in jobs.items():
        if name == release_job:
            continue
        if not isinstance(follower, dict):
            problems.append(f"release job {name!r} is not a mapping")
            continue
        seen: set[str] = set()
        frontier = _needs(follower)
        while frontier:
            current = frontier.pop()
            if current in seen:
                continue
            seen.add(current)
            upstream = jobs.get(current)
            if isinstance(upstream, dict):
                frontier.extend(_needs(upstream))
        if release_job not in seen:
            problems.append(f"release job {name!r} must depend on {release_job!r} through needs")
        if "if" in follower:
            problems.append(f"release job {name!r} declares if and can be skipped")
        value = follower.get("continue-on-error")
        if value is not None and value is not False:
            problems.append(f"release job {name!r} has continue-on-error: {value!r}")
    return problems
