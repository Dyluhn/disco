"""Interior modules of `ProcessSandboxInstance` (`..process`).

`ProcessSandboxInstance` owns ONE authority — a jailed, per-instance workspace
on the local host — and composes it from cohesive pieces this package holds:

* `secure_io` — the no-follow, directory-descriptor-walked `read_file` /
  `delete_file` implementations (the workspace-jail enforcement for raw file
  I/O; every OSError-to-SandboxPermissionError mapping is preserved verbatim
  and in order).
* `workspace_rewrite` — the ROOT-1 `/workspace`-token shell-command rewrite,
  jailed through the SAME `_resolve` the file tools use.

Every function takes the live `ProcessSandboxInstance` (or its resolved
`_workspace`/`_resolve`) explicitly; nothing here is a general-purpose helper
divorced from the instance whose jail it enforces.
"""

from __future__ import annotations
