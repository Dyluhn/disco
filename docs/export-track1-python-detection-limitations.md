# Track-1 Python release-detection — accepted limitations & reopen criterion

Status: **CLOSED** (Batch-1 correction loop closed at correction #14; cleanup pass green).
Scope: the static Python release-detection proof — `packages/core/src/disco/core/release/python_proof.py`
and its production consumer `detect.py`. This document records what the proof does *not*
decide, why that is acceptable, and the **only** condition under which Batch 1 may be reopened.

## What the proof is (and is not)

The Python proof is a **static, import-abort pre-filter**. It statically parses the
candidate app's module graph and rejects (→ `needs_review`) only what it can prove will
**abort at import time with static certainty** (an unconditional/statically-reachable
`raise`/`sys.exit`/`os._exit`/`os.abort`/`os.kill(getpid, …)` that no guard of sufficient
tier catches). Everything else it leaves as a **candidate**.

**A Track-1 `candidate` is explicitly UNVERIFIED.** There is *no* production boot probe,
readiness probe, or runtime check downstream that confirms a candidate actually starts and
serves. "Candidate" means "the static pre-filter found no proof-of-abort" — nothing more.
The proof's guarantee is one-directional and deliberately conservative:

- **False candidate** (an app that cannot run, passed as `candidate`) = **CRITICAL**. The
  correction loop (#8–#14) drove the realistic false-candidate rate to **zero** across the
  13-real-app surface and the 104-case adversarial matrix.
- **Over-rejection** (a runnable app pushed to `needs_review`) = **safe direction**. The
  proof errs here on ambiguous/dynamic constructs rather than risk a false candidate.

## Accepted limitations — the dynamic boundary

The proof decides only **statically certain** outcomes. The following are **out of scope by
design** and left as UNVERIFIED candidates (or conservatively over-rejected); they are
**accepted Track-1 limitations**, not defects:

1. **Runtime-only exceptions that are not statically foldable.** `1/0`, `int("x")`, `[][0]`
   (IndexError), `{}["k"]` (KeyError), `open("/nonexistent")`, attribute errors, and any
   `NameError` from a name whose binding cannot be statically resolved. These raise only when
   executed; the static proof does not evaluate them and leaves the module a candidate.
2. **Control flow gated on runtime state.** A `raise`/exit reachable only under a condition
   the proof cannot fold statically — e.g. `if os.environ.get("X"): raise` — stays a
   candidate (the abort is not statically certain).
3. **Narrow-handler conservatism.** A `raise` swallowed only by a *narrow* handler
   (`except ValueError:`, `except* ValueError:`) is **not** credited as caught — only
   representative catchers (`Exception`/`BaseException`, tier ≥ EXCEPTION) are credited. Such
   modules become `needs_review` (over-rejection, safe). Regular `except` and `except*`
   behave identically here.
4. **Alias/rebind and dynamic dispatch beyond static resolution.** Terminating-call aliasing
   is resolved to a bounded static depth (`_resolved_call_name`/`_terminating_aliases`);
   deeper indirection, `getattr`-dispatched exits, `exec`/`eval`, and metaprogramming are not
   statically decided and stay candidates.
5. **Exit-code masking.** A dependency that calls `os._exit(0)` at import exits the process
   with **return code 0**. The static proof catches this via the UNCATCHABLE guard tier, but
   any *downstream* backstop that trusts an exit code alone would be fooled — see the R7
   requirement in [`export-track1-r7-docker-lane-requirements.md`](./export-track1-r7-docker-lane-requirements.md).

These limitations are the documented **dynamic boundary**: static structural certainty is
in scope; runtime-dependent failure is not. This is the correct boundary for an
import-abort pre-filter and matches the convergent invariant established across corrections
#8–#14 — every fold / reachability / abort predicate mirrors `_static_truthiness` (its 7
literal-folding arms: Constant, Tuple/List/Set display, Dict display, UnaryOp-Not, BoolOp,
Subscript, Compare).

## Reopen criterion (binding)

**Batch 1 may be reopened ONLY if an ordinary application fails** — specifically, if a
realistic **FastAPI, Flask, Starlette, raw-ASGI, or factory-pattern** application is
misclassified (a runnable real app pushed to `needs_review`, or — far more serious — an
unrunnable one passed as `candidate`).

**Contrived Python AST attacks that differ from real apps only in the dynamic-boundary
constructs above (items 1–5) are OUT of scope and must NOT reopen Batch 1.** The adversarial
matrix already covers the realistic surface; further AST fuzzing of exotic dynamic constructs
is explicitly not a reopen trigger. Adversary #12 rated the realistic surface **sound**
(0 realistic false candidates, 0 realistic over-rejections across 13 real apps) and the proof
**fit-for-purpose** as a pre-filter.

## Verified end-state (evidence)

- Focused proof suite: **563 passed / 0 failed** — `test_r7_python_proof.py` +
  `test_r7_python_proof_reopen.py` (junit-confirmed `tests=563`).
- Broader remediation suite (full `release_remediation/` minus the parity file):
  **696 passed** (this corrects the earlier stale count of 654, which predated the
  regressions added in corrections #12–#14).
- 104-case independent adversarial matrix: exactly **13 reproduced, all non-Python**
  (EXT-01, HEALTH-01, LEX-01/02/03, MIGRATE-01..04, SERVER-06, SPEC-02, SYNTAX-01/02),
  **zero PY*/REQ***, source stable during probe.
- Gates: `ruff check` clean, `ruff format --check` clean, `basedpyright` 0/0/0.
- The enforceable, re-runnable certification is `scripts/batch1_final_receipt.py`
  (fingerprints the worktree, derives HEAD from git, runs the 563 tests + 104 matrix,
  exits nonzero unless every result is exact). NOTE: this receipt pins the Batch-1-era
  `python_proof.py` hash; the Batch-3 called-scope work below legitimately changed that file,
  so the receipt's pin is intentionally stale and is re-baselined at Round-5 final verification.

## Called-scope import resolution — the indirect-call boundary (Batch-3 rounds 3–6)

Batch 3 extended the proof so a declared **migration script** (`migrate_cmd = ("python", …)`)
and the **entrypoint module** must have every import they will execute at load time *resolved
against the exact install plan ∪ shipped workspace modules ∪ stdlib*. A migration/entrypoint
that imports a package absent from `requirements.txt` now grades `needs_review` (previously a
false `candidate` that crashed the migrate one-shot → gated the ingress off → unreachable
deployment). This is enforced by a **certain-evaluation walker** (`_record_statement_calls` /
`_certain_calls_in_expr` in `python_proof.py`): it attributes the imports of a module-scope
local function whenever a call to it is **statically certain to execute at module load** —
across the full *direct-call grammar* (call func + args, decorators, class-body statements,
`with`-items, `for`-body over a non-empty **inline literal**, eager comprehension element,
f-string, left operand of `and`/`or`, the taken arm of a static ternary, `assert` test,
first compare operand, `def` defaults) — with **`try`-body guard-tier composition**: an import
reached under a handler that swallows `ImportError`/bare-`except`/`Exception` stays a candidate;
one under a non-catching or catching-but-aborting handler (`except ConnectionError:`,
`except ModuleNotFoundError: sys.exit(1)`) is required.

**Ordinary placements are caught** (all → `needs_review`, execution-matched exit 1): a
module-top `import`, an import at the top of an imported submodule (transitive), an import in a
module-scope-called function, a decorator, a declarative class body, an argument-position call,
`app = create_app()` / `configure(app)` / `--factory`, and an inline-literal loop.

### Accepted residuals — the indirect-call / points-to boundary

These are **under-attribution** cases: a `candidate` that cannot run, but **only** when the
missing import is **relocated into a function or method reached indirectly** (never through a
statically-certain direct call). In every case the *ordinary* placement of the same
forgotten-dependency bug (module-top import, or a directly-called function) **is caught** — the
leak survives only the less-common lazy-import-inside-an-indirectly-reached-callable style. They
are accepted boundaries, not defects, because resolving them requires alias / points-to / method-
dispatch / lazy-evaluation modeling — **not** the finite call grammar:

6. **Method / constructor dispatch.** `C()` or `C().m()` where `__init__`/`m` imports the
   missing package. Requires `C()`→`__init__`/MRO modeling.
7. **Name alias & higher-order callback.** `s = run; s()`; `map(fn, …)`; `list(fn() for …)` and
   other eager consumers of a generator whose element calls a local (the generator element is
   correctly treated as lazy for a bare genexp — consumption tracking is the gap); `sorted(…,
   key=lambda a: h(a))`. Requires alias/points-to + consumption modeling.
8. **Non-literal-but-constant iterables.** `for t in TABLES: helper(t)` where `TABLES` is a
   module-level constant list, or `for t in range(3): helper(t)` — the loop body is descended
   only for an **inline** non-empty literal, so a provably-constant `Name` or `range(<literal>)`
   is not folded. This is the **closest-to-ordinary residual**. A **sound, bounded, non-over-
   rejecting fold-fix is available and RECOMMENDED as optional defense-in-depth**: fold a
   `for`/comprehension iterable that is either a module-scope `Name` bound exactly once to a
   non-empty literal display and never rebound/mutated, or a `range()` with a provably-positive
   integer-literal argument, then descend the body. It is deferred (not implemented) because the
   ordinary forgotten-dependency bug is already caught in its module-top form, and further
   iterable forms (`A + B` of constants, `list(X)`, dynamic iterables) extend the boundary past a
   bounded fold — i.e. this is the point at which the finite grammar ends and open-ended constant
   propagation begins.

### Convergence evidence

Rounds 3→4→5→6 each closed a strictly deeper shape of "resolve what actually executes at module
load", walking the finite direct-call grammar to its fixpoint. The round-6 **independent,
execution-backed** adversary (every verdict cross-checked against real `python -I` / `import`
exit codes) confirmed: the finite certain-eval grammar is **closed**, guard-tier composition is
exactly sound against the live runtime, non-Python (port/bind/health/node-migrate) is solid, and
**no ordinary class-A false candidate exists** — every constructible false candidate requires the
indirect-relocation boundary above (items 6–8), while every ordinary placement is caught. Batch 3
**converged** on that verdict. The reopen criterion is identical to Batch 1's: an ordinary
migration or FastAPI/Flask/Starlette/ASGI/factory application (a real forgotten dependency in its
ordinary module-top or directly-called form) misclassified as `candidate` reopens; contrived
relocation into method/alias/lazy/constant-iterable indirection (items 6–8) does not.

## Health-path proof — the registration-vs-reachability boundary (Batch-4)

`_health_route_present` (`detect.py`, python whole-tree scan) and `_js_serves_path`
(entrypoint-view, node) certify that the declared `health_path` corresponds to an **explicit route
registration** — a decorator/`Route(...)`/`.get('<path>', …)` literal on some router or app object.
This is the ratified C3 contract ("an explicit route registration certifies it"): the catch-all
`createServer`, the stray docs literal, the commented-out route, and the templated/computed path all
correctly fail closed to `health_path_unresolved`.

### Accepted residual — registration ≠ mounting

9. **Route registered but never mounted.** A `@router.get('/healthz')` whose `router` is never
   `app.include_router(router)`'d (python), a route defined only in an **unimported** module, or a
   node `router.get('/healthz', …)` whose router is never `app.use`'d — each grades `candidate`
   even though `GET /healthz` 404s on the served app. The health proof certifies **registration**,
   not **mounting/reachability**. This is an accepted boundary, not a Batch-4 defect, on three
   independent grounds:

   - **Operational severity is not class-A.** The emitted compose gives the ingress
     `restart: unless-stopped` + a `healthcheck` block, but **no service declares
     `condition: service_healthy`** (`local_compose.py` emits only `service_completed_successfully`
     for the migrate one-shot and `service_started` for generic deps). `restart: unless-stopped`
     acts on process *exit*, not health status, so an unhealthy container **keeps running and keeps
     serving** — the ordinary app still boots and serves `GET /`; only the declared probe path
     404s and the operator's health dashboard reads red. Contrast the migrate one-shot
     (`restart: no` + ingress `depends_on migrate: service_completed_successfully`), where a crash
     makes the whole deployment unreachable — *that* is the class-A shape; a wrong health probe is
     not.
   - **Overriding it contradicts the ratified C3 oracle.** The frozen C3 detection-matrix tests
     require an explicit registration to certify; requiring proof of *mounting* would rewrite the
     ratified contract (an owner decision), not remediate a defect within it.
   - **Proving mounting requires points-to analysis.** Deciding which `APIRouter`/`Router` object
     flows into `include_router()`/`app.use()` (possibly imported from another module, aliased,
     subclassed, or `app.mount`ed) is an alias/data-flow problem — the same non-finite points-to
     boundary as items 6–8, not a finite-grammar closure. A naive same-file "is this name
     include_router'd?" check would **over-reject the single most common ordinary FastAPI layout**
     (route in `routers/*.py`, `include_router`'d in `main.py`), trading a benign wrong-probe
     residual for a real safe-direction over-rejection of ordinary apps — a net regression.

   The ordinary correct shapes are all certified (`@app.get`/decorator direct, router **mounted**
   via `include_router`/`app.use`, Flask blueprint, Starlette `Route(...)`), and the reject-set the
   health proof was built for (catch-all / stray literal / comment / computed path) all fail closed.
   Batch 4 **converged** on this verdict (EXT-01 and SYNTAX-01/02 clean in both directions, HEALTH-01
   over-rejections exotic/safe-direction); the registration-vs-reachability ceiling is surfaced for
   owner ratification at commit, not silently suppressed and not manufactured into a hard defect.

## Gunicorn config proof — the config-runtime-abort boundary (Batch-5)

SERVER-06's root cause is that gunicorn **executes its config file as a Python module at startup**, so
existence is not runnability. The proof (`_gunicorn_config_blocker` in `detect.py`) gives the config the
SAME proof the migrate script gets — scan-complete fail-closed → `ast.parse` → the Batch-3 certain-eval
import resolution (`python_script_import_error` → `_module_imports_are_proven`) over install-plan ∪ shipped
workspace ∪ stdlib — applied to **every** path gunicorn loads the config from: the explicit `--config`/`-c`
file AND the **auto-discovered** `<cwd>/gunicorn.conf.py` (gunicorn `config.get_default_config_file()`,
loaded by `app.base.load_config()`'s else-branch when no `--config` is given; cwd = the `--chdir` dir else
the repo root, that directory only — never a subdir). This catches the ordinary class-A defects: an
unparseable config (SyntaxError → boot abort) and a config with a forgotten dependency (module-scope import
of a distribution absent from the install plan → `ModuleNotFoundError` at boot) — via BOTH resolution paths.

### Accepted residual — the config runtime-abort boundary

10. **A config that scans, parses, and whose imports all resolve, but which raises/exits at module
    load** — e.g. an unconditional `raise RuntimeError(...)` or `sys.exit(1)` at the top of
    `gunicorn.conf.py`, or a runtime-conditional abort (`if os.environ.get("X"): raise`), or a
    non-statically-foldable runtime error (`1/0`) — still grades `candidate` even though gunicorn would
    abort at startup. The config proof reuses `python_script_import_error`, which **deliberately omits the
    unconditional module-scope abort check** (its docstring: a migration script "legitimately runs to
    completion and may `sys.exit(0)`", so it checks ONLY that imports resolve). This is an accepted
    boundary, not a Batch-5 defect, on two grounds:

    - **Not ordinary.** Real `gunicorn.conf.py` files set `bind`/`workers`/`worker_class` and import
      stdlib (`multiprocessing`) or a shipped helper; they do not `raise` or `sys.exit` at module load.
      The reopen criterion is "an *ordinary* application misclassified"; a config that unconditionally
      aborts at load is exotic. (Note the semantic wrinkle recorded honestly: for a migrate one-shot
      `sys.exit(0)` is legitimate success, whereas for a config it would abort gunicorn — so the shared
      primitive's rationale does not transfer perfectly; ordinariness, not semantics, is what makes this
      an acceptable residual.)
    - **Same dynamic boundary as items 1–2.** Runtime-dependent aborts and non-statically-foldable runtime
      errors are the documented dynamic boundary of the whole static approach; the config inherits it.

    Every ordinary config bug **is** caught: unparseable body (both paths), forgotten dependency (both
    paths, incl. under `--chdir`), while every ordinary correct config stays a candidate — the canonical
    `import multiprocessing; workers = multiprocessing.cpu_count()*2+1`, `import gunicorn`, a shipped-
    workspace-module import, a `try/except`-guarded optional import, valid/empty/comment configs, a broken
    config in a **subdir** (gunicorn does not auto-load subdirs), and a broken **root** config when
    `--chdir` points elsewhere (gunicorn auto-loads only the chdir'd dir). The runtime-abort residual is
    surfaced for owner awareness and flagged to the round-2 adversary for independent assessment, not
    silently suppressed and not manufactured into a hard defect.

### The config-worker_class rule (Batch-5 round 3) — mirror the ratified CLI ceiling

gunicorn loads `worker_class` at `Arbiter.__init__` → `setup()` via `util.load_class(...)` **before** it binds a
socket: `worker_class = 'gevent'` imports `gunicorn.workers.ggevent`, whose body is `try: import gevent except
ImportError: raise RuntimeError(...)`. So a config selecting a non-bundled worker whose dependency is absent aborts
the master before any worker binds → unreachable. The command grammar already restricts the CLI `--worker-class`
(and its `-k` short form) for gunicorn to the built-in proven set `{sync, gthread}` (a ratified oracle,
`command_grammar.py`); the config `worker_class = <string>` was bypassing it. The fix lifts that set to a single
shared constant `GUNICORN_BUILTIN_WORKER_CLASSES` (read by BOTH the CLI validator and the config proof, with a
drift-guard test) and, inside `_gunicorn_config_blocker`, blocks a module-scope `worker_class = <string-literal>`
(`ast.Assign`/`ast.AnnAssign`) not in that set → `needs_review`, on both the explicit `--config`/`-c` and the
auto-discovered paths. It **mirrors the CLI exactly** — `worker_class='gevent'` is rejected even when gevent IS in
requirements, because the ratified CLI rejects it unconditionally; allowing it in the config would make the config
more permissive than the CLI (an inverse asymmetry contradicting the oracle). Widening to async workers (e.g.
`uvicorn.workers.UvicornWorker`) is an owner decision to loosen BOTH surfaces together, not a unilateral config change.

11. **Other boot-imported string settings & non-literal `worker_class`.** `logger_class = '<dotted>'` and
    `wsgi_app = '<module:attr>'` are also strings gunicorn may import at startup, and a `worker_class` whose value is
    a non-literal (a name/alias `wc='gevent'; worker_class=wc`, a `str`-concat `'gev'+'ent'`, an `os.environ.get(...)`,
    or a function-scope binding) is not statically resolvable to a string. These stay `candidate` and are accepted
    boundaries, on two grounds: (a) **not ordinary** — `logger_class`/`wsgi_app` are rarely set (defaults are the
    bundled `gunicorn.glogging.Logger` and the CLI/`--config` target), unlike `worker_class` which is textbook and had
    a ratified CLI ceiling to mirror; and there is **no ratified CLI rule** for `logger_class`/`wsgi_app` to hold the
    config to; (b) a non-literal `worker_class` is the documented **dynamic/alias boundary** (items 1–2, 6–8) — the
    ordinary form (a direct string-literal) is caught, only the contrived indirection evades it. Config hook functions
    (`post_fork`, `when_ready`, …) that import a missing dependency are lower-severity: gunicorn calls them **after**
    the socket binds, so the deployment is reachable when they fail. All are surfaced for owner awareness and flagged
    to the round-3 adversary, not silently suppressed and not manufactured into hard defects.
