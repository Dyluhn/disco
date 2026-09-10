"""Interior modules of `GvisorSandboxService` (`..gvisor`).

`GvisorSandboxService` owns ONE authority — the Docker/OCI backend's `create`
path (`LocalSandboxService` reuses this whole class; `PodmanSandboxService`
mirrors the shape independently) — and composes it from cohesive pieces this
package holds:

* `egress_setup` — `_setup_filtered_egress`'s dual-homed sidecar + policy
  proxy provisioning (the three VM-201-verified non-obvious details: boot-time
  dual NICs, a working resolver, and reaching the proxy by IP).
* `container_start` — `_start_container`'s egress setup → volume → container
  run → inbound-forwarder sequence, with start-failure cleanup.
* `conversation_cleanup` — `destroy_by_conversation`'s dual-prefix,
  dual-label sweep of containers/networks/volumes for one conversation.

Every jail/capability/egress check and every cleanup step is preserved
verbatim and in the same order as the class body this was extracted from.
Functions take the live `GvisorSandboxService` explicitly and call back
through it (`svc._launch_capability_relay(...)`, `svc._best_effort_cleanup(...)`)
so an instance-level test override still takes effect when the call
originates here instead of the class body.
"""

from __future__ import annotations
