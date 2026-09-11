"""Put the published sandbox image into the host daemon under its canonical name.

Runs once per `compose up` as the `sandbox-image` service. The sandbox backends
never pull at run time (a missing image is a typed error) and the Settings
default names the image `disco-sandbox:base`, a name a registry pull cannot
produce. So: pull the published image when the daemon does not have it, then
tag it with the canonical name. Idempotent; exits 0 when nothing needs doing.
"""

from __future__ import annotations

import os
import sys

import docker
from docker.errors import ImageNotFound


def _split(reference: str) -> tuple[str, str]:
    """`ghcr.io/x/disco-sandbox:v0.2.0` -> (`ghcr.io/x/disco-sandbox`, `v0.2.0`)."""
    repository, separator, tag = reference.rpartition(":")
    if not separator or "/" in tag:
        return reference, "latest"
    return repository, tag


def main() -> int:
    source = os.environ.get("DISCO_SANDBOX_IMAGE_SOURCE", "")
    if not source:
        print("ensure_sandbox_image: DISCO_SANDBOX_IMAGE_SOURCE is not set", file=sys.stderr)
        return 2
    target = os.environ.get("DISCO_SANDBOX_IMAGE") or "disco-sandbox:base"
    socket = os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock")
    # A first pull is about 3 GB; the SDK default timeout (60 s) is far too short.
    client = docker.DockerClient(base_url=socket, timeout=3600)
    try:
        image = client.images.get(source)
        print(f"ensure_sandbox_image: {source} already present")
    except ImageNotFound:
        print(f"ensure_sandbox_image: pulling {source} (about 3 GB on first run)", flush=True)
        repository, tag = _split(source)
        image = client.images.pull(repository, tag=tag)
    repository, tag = _split(target)
    image.tag(repository, tag)
    print(f"ensure_sandbox_image: {target} -> {image.id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
