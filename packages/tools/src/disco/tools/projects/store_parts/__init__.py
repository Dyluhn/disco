"""Interior modules of `..store` (`ProjectStore` and its collaborators).

`store.py` remains the authority: `ProjectStore` is defined there (its public
surface — the rollback adapter other packages depend on — is unchanged), and
these modules hold the cohesive interior pieces it and its collaborators
compose, split out to keep the parent module's logical-line and class-size
budgets in check:

* `fs_proof` — low-level fd-anchored stat-identity/hash primitives.
* `json_atomic` — durable JSON write/read (crash-safe tmp+rename, no-follow).
* `tree_scan` — workspace tree hashing (mutable mirror + immutable version).
* `version_serde` — `VersionRecord` JSON (de)serialization + exact-shape proof.
* `verified_reader` — `_VerifiedVersionReader`: strict version reading and
  fd-proof trees.
* `version_coordinator` — `_VersionStateCoordinator`: the never-reuse
  sequence allocator, the WAL-protected pin/unpin journal, and version
  life-cycle (create/cut/prune).
* `project_store_ops` — free functions behind `ProjectStore`'s thinner CRUD
  and locking methods.

`_write_json_atomic` is the one name here a test monkeypatches directly on
the `store` facade module (`store_module._write_json_atomic`). Every call
site to it in these modules therefore resolves it through `store`'s OWN
binding at call time (a local `from disco.tools.projects import store` import
immediately before the call), never a top-of-file `from ..store import
_write_json_atomic` — the latter would capture the pre-patch function object
and silently defeat the test. Every other cross-reference to a `store.py`-
defined name (constants, exception/dataclass types, non-monkeypatched
helpers) is a plain top-of-file import; `store.py` defines all of them before
importing from this package, so the ordering is safe.
"""

from __future__ import annotations
