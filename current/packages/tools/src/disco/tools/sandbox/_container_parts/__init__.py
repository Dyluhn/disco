"""Interior modules of the shared container-backend logic (`.._container`).

`ContainerInstance` (the gVisor/Podman shared session/jail/exec behavior) and
the module-level helpers around it own ONE authority — the container-backend
half of tool-sandbox-contract §5.1 — and compose it from cohesive pieces this
package holds:

* `guest_scripts` — the embedded, stdlib-only Python helpers (`_BOUNDED_*`)
  that run INSIDE the guest for exec/read/delete/list, plus the argv builders
  and result parsers around them. Moved here purely to shed module-size
  budget: embedded script text counts as logical source.
* `atomic_write` — `ContainerInstance.atomic_write`'s staged, SELinux-safe
  commit-via-rename.
* `failure_classification` — the wedge-guarded reload + death-vs-transient
  classification (`_safe_reload`, `_classify_failure_*`, `_confidently_alive`).
* `lifecycle` — `destroy` + the shared egress-aux teardown.
* `port_mapping` — `_resolve_mapping`'s Docker/Podman port-binding read.
* `aux_reconcile` — `reconcile_orphan_aux_resources`'s dual-prefix orphan
  network/volume sweep.

Every jail/capability/egress check and every cleanup step is preserved
verbatim and in the same order as the module/class body this was extracted
from. Functions that need the live `ContainerInstance` take it explicitly and
call back through it (`instance._guest_run(...)`) so an instance-level test
override still takes effect when the call originates here instead of the
class body. Functions that read a name mutated at the `.._container` module
level (a monkeypatched stdlib module attribute, e.g. `subprocess.Popen`) do a
plain `import subprocess` themselves — mutating the real shared module object
affects every importer identically, so no deferred-access indirection is
needed for that class of patch.
"""

from __future__ import annotations
