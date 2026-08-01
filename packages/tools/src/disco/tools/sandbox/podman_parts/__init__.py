"""Interior modules of `PodmanSandboxService` (`..podman`).

`PodmanSandboxService` owns ONE authority — the Podman native-remote `create`
path (the podman-py-vs-docker-py transport split documented in `..podman`'s
module docstring) — and composes it from cohesive pieces this package holds:

* `egress_setup` — `_setup_filtered_egress`'s dual-homed sidecar + policy
  proxy provisioning (spirit-identical to `gvisor_parts.egress_setup`, with
  the podman-specific CLI-exec + put_archive transport).
* `container_start` — `_start_container`'s image-present check → egress setup
  → volume → container create/start → inbound-forwarder sequence, with
  start-failure cleanup.
* `conversation_cleanup` — `destroy_by_conversation`'s dual-prefix,
  dual-label sweep of containers/networks/volumes for one conversation.

Every jail/capability/egress check and every cleanup step is preserved
verbatim and in the same order as the class body this was extracted from.
Functions take the live `PodmanSandboxService` explicitly and call back
through it (`svc._launch_capability_relay(...)`, `svc._best_effort_cleanup(...)`)
so an instance-level test override still takes effect when the call
originates here instead of the class body.
"""

from __future__ import annotations
