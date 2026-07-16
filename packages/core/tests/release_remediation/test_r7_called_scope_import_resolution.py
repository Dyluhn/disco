"""Correction Round 5 — the top-level module's UNCONDITIONALLY-CALLED local functions are now
part of the import-resolution proof, and ``if TYPE_CHECKING:`` bodies are statically dead.

Two proven defects closed here, both in the single authoritative static import proof
(``python_proof.py``) reused by BOTH the migrate-script proof (``python_script_import_error``) and
the entrypoint proof (``python_entrypoint_error``):

DEFECT 1 (class-A, the reopener) — ``_ScopeImportVisitor`` deliberately treats nested scopes as
opaque, which is SOUND for a module merely imported-for-an-attribute (you do not run a library's
functions by importing it) but UNSOUND for the TOP-LEVEL EXECUTED module: a ``python <script>`` runs
top-to-bottom and an entrypoint module is imported by uvicorn so its body executes, and a
module-scope call to a LOCAL function runs that function's body — so imports inside it really fire.
The fix proves the imports of every module-scope local function the top-level module UNCONDITIONALLY
calls, following the call chain recursively (bounded by ``_MAX_LOCAL_IMPORT_DEPTH`` + a visited-set)
and reusing the SAME reachability/``_static_truthiness`` machinery so dynamic-guarded calls stay
unattributed (the documented dynamic boundary). The ``if __name__ == '__main__':`` context is folded
per caller: a ``python <script>`` runs as ``__main__`` (True — the block is reachable) while an
entrypoint imported by uvicorn is not (False — its ``__main__`` block is dead on import).

DEFECT 2 (ordinary over-rejection) — ``if TYPE_CHECKING:`` guarded imports were required even though
``typing.TYPE_CHECKING`` is False at runtime and the import never fires. The fix folds a reference
to ``TYPE_CHECKING`` (bare name or the exact ``typing.TYPE_CHECKING`` attribute) to False, so the
guarded body is statically dead and its imports are not required — the standard static convention.

Lives OUTSIDE the frozen ``export_track1_closeout`` dirs (no marker), so it never perturbs the
acceptance manifest.
"""

from __future__ import annotations

import pytest
from disco.core.release.detect import Provenance, detect_release
from disco.core.release.spec import (
    LocalResourceProfile,
    ReleaseAssessment,
    ReleaseIntent,
    ResourceDecl,
    ResourceKind,
    ResourceProfiles,
    RuntimeStrategy,
)

_PY_APP = "from fastapi import FastAPI\napp = FastAPI()\n"
_ABSENT = "zzz_totally_absent_pkg_9999"

# Distinctive, greppable message substrings the two proofs emit (histogram keys).
_MIGRATE_IMPORT_MSG = "not backed by the install plan or a shipped workspace module"
_ENTRY_DEP_MSG = "python target uses an unproven import or global dependency"
_ENTRY_SHAPE_MSG = "no closed server-compatible application shape"


def _migration_resource(command: tuple[str, ...]) -> ResourceDecl:
    return ResourceDecl(
        id="db",
        kind=ResourceKind.sqlite,
        persistent_path="/data/app.db",
        profiles=ResourceProfiles(
            local=LocalResourceProfile(url="file:/data/app.db", volume="app-data")
        ),
        consumers=("web",),
        migrate_cmd=command,
    )


def _migrate_intent(command: tuple[str, ...]) -> ReleaseIntent:
    return ReleaseIntent(
        runtime=RuntimeStrategy.python,
        start_cmd=("uvicorn", "main:app"),
        resources=(_migration_resource(command),),
    )


def _pmig(migrate_body: str) -> tuple[dict[str, str], ReleaseIntent]:
    """A migrate script run as ``python migrate.py`` (top-level ``__main__``)."""
    files = {
        "requirements.txt": "fastapi\nuvicorn\n",
        "main.py": _PY_APP,
        "migrate.py": migrate_body,
    }
    return files, _migrate_intent(("python", "migrate.py"))


def _pentry(main_body: str, start_cmd: tuple[str, ...] = ("uvicorn", "main:app")):
    """An entrypoint module IMPORTED by uvicorn (its body executes on import)."""
    files = {"requirements.txt": "fastapi\nuvicorn\n", "main.py": main_body}
    return files, ReleaseIntent(runtime=RuntimeStrategy.python, start_cmd=start_cmd)


def _run(files: dict[str, str], intent: ReleaseIntent):
    return detect_release(files, intent=intent, provenance=Provenance())


def _assert_needs_review(
    files: dict[str, str], intent: ReleaseIntent, code: str, message_substr: str
) -> None:
    result = _run(files, intent)
    assert result.assessment is ReleaseAssessment.needs_review, [b.message for b in result.blockers]
    assert [b.code for b in result.blockers] == [code], [b.code for b in result.blockers]
    assert any(message_substr in b.message for b in result.blockers), (
        f"expected {message_substr!r} in {[b.message for b in result.blockers]}"
    )


def _assert_candidate(files: dict[str, str], intent: ReleaseIntent) -> None:
    result = _run(files, intent)
    assert result.assessment is ReleaseAssessment.candidate, [b.message for b in result.blockers]


# ===========================================================================
# DEFECT 1 — imports inside a module-scope-called local function (false candidates)
# ===========================================================================


def test_a1_migrate_calls_local_fn_importing_absent_pkg_fails_closed() -> None:
    """A1: ``run()`` is called at module scope; its body ``import <absent>`` really executes and
    ModuleNotFound-crashes the one-shot migration. Must fail closed."""
    files, intent = _pmig(
        f"def run():\n    import {_ABSENT}\n    {_ABSENT}.create_engine('sqlite://')\nrun()\n"
    )
    _assert_needs_review(files, intent, "migrate_target_unresolved", _MIGRATE_IMPORT_MSG)


def test_a5_migrate_alembic_idiom_in_dunder_main_fails_closed() -> None:
    """A5: the canonical alembic idiom — ``def main(): from alembic import command ...`` called from
    ``if __name__ == '__main__':``. As a ``python <script>`` the module IS ``__main__``, so the
    block is reachable and ``main()`` runs; alembic absent from requirements must fail closed."""
    files, intent = _pmig(
        "import os\n"
        "def main():\n"
        "    from alembic import command\n"
        "    command.upgrade(None, 'head')\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
    )
    _assert_needs_review(files, intent, "migrate_target_unresolved", _MIGRATE_IMPORT_MSG)


def test_e1_entrypoint_calls_local_setup_importing_absent_pkg_fails_closed() -> None:
    """E1: an entrypoint whose module-scope ``_setup()`` imports a package absent from the install
    plan. uvicorn imports the module, ``_setup()`` runs, ModuleNotFound. Must fail closed."""
    files, intent = _pentry(
        f"from fastapi import FastAPI\n"
        f"def _setup():\n    import {_ABSENT}\n    return {_ABSENT}\n"
        f"_setup()\n"
        f"app = FastAPI()\n"
    )
    _assert_needs_review(files, intent, "entrypoint_unresolved", _ENTRY_DEP_MSG)


def test_nested_unconditional_call_chain_is_followed() -> None:
    """The call chain is followed recursively: c() -> b() -> a(), and a() imports the absent pkg."""
    files, intent = _pmig(
        f"def a():\n    import {_ABSENT}\ndef b():\n    a()\ndef c():\n    b()\nc()\n"
    )
    _assert_needs_review(files, intent, "migrate_target_unresolved", _MIGRATE_IMPORT_MSG)


def test_migrate_dunder_main_block_absent_import_fails_closed() -> None:
    """A ``python <script>`` runs as ``__main__``, so a direct ``import <absent>`` inside its
    ``if __name__ == '__main__':`` block executes and must fail closed."""
    files, intent = _pmig(f"if __name__ == '__main__':\n    import {_ABSENT}\n")
    _assert_needs_review(files, intent, "migrate_target_unresolved", _MIGRATE_IMPORT_MSG)


# ===========================================================================
# DEFECT 2 — TYPE_CHECKING-guarded imports are statically dead (ordinary over-rejection)
# ===========================================================================


def test_b4_type_checking_guarded_import_is_candidate() -> None:
    """B4: ``if TYPE_CHECKING: import <absent>`` never runs (TYPE_CHECKING is False at runtime), so
    the import must not be required — an ordinary runnable app, must be candidate."""
    files, intent = _pmig(
        f"from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import {_ABSENT}\nprint('ok')\n"
    )
    _assert_candidate(files, intent)


def test_typing_attribute_type_checking_guard_is_candidate() -> None:
    """The ``typing.TYPE_CHECKING`` attribute form folds to False identically."""
    files, intent = _pmig(
        f"import typing\nif typing.TYPE_CHECKING:\n    import {_ABSENT}\nprint('ok')\n"
    )
    _assert_candidate(files, intent)


def test_statically_false_if_body_import_is_candidate() -> None:
    """A general ``if False:`` (and any statically-false test) body never executes: its import must
    not be required. This is the reachability that makes the TYPE_CHECKING fold effective."""
    _assert_candidate(*_pmig(f"if False:\n    import {_ABSENT}\nprint('ok')\n"))
    _assert_candidate(*_pmig(f"if 1 == 2:\n    import {_ABSENT}\nprint('ok')\n"))


# ===========================================================================
# CONTROLS — must NOT change / must NOT over-reject an ordinary runnable app
# ===========================================================================


def test_control_top_level_absent_import_still_fails_closed() -> None:
    """A1-CONTROL: the same absent import at MODULE scope stays needs_review (the module-scope proof
    is unchanged and still authoritative)."""
    files, intent = _pmig(f"import {_ABSENT}\n{_ABSENT}.create_engine('sqlite://')\n")
    _assert_needs_review(files, intent, "migrate_target_unresolved", _MIGRATE_IMPORT_MSG)


def test_control_uncalled_local_function_import_not_required() -> None:
    """UNCALLED: a dev helper never called must not have its imports required (candidate)."""
    _assert_candidate(
        *_pmig(f"def _dev():\n    import {_ABSENT}\n    {_ABSENT}.set_trace()\nprint('ok')\n")
    )


def test_control_function_called_only_from_uncalled_function_not_required() -> None:
    """A function reached ONLY from an uncalled function is itself never run — not required."""
    _assert_candidate(*_pmig(f"def a():\n    import {_ABSENT}\ndef b():\n    a()\nprint('ok')\n"))


def test_control_dynamic_guard_call_is_not_unconditional() -> None:
    """A6: a call behind a DYNAMIC condition (``if os.environ.get('X'):``) is not unconditional, so
    its function's imports are the documented dynamic boundary — stays candidate."""
    _assert_candidate(
        *_pmig(f"import os\ndef run():\n    import {_ABSENT}\nif os.environ.get('X'):\n    run()\n")
    )


def test_control_try_body_call_is_conservative() -> None:
    """A call inside a ``try`` body is NOT attributed (a handler could swallow the ImportError), the
    conservative boundary — stays candidate."""
    _assert_candidate(
        *_pmig(f"def run():\n    import {_ABSENT}\ntry:\n    run()\nexcept Exception:\n    pass\n")
    )


def test_control_import_error_guard_stays_candidate() -> None:
    """GUARD: the ``try: import ujson except ImportError: import json`` optional-dependency idiom at
    module scope stays candidate (guard-tier machinery, unchanged)."""
    _assert_candidate(
        *_pmig("try:\n    import ujson as json\nexcept ImportError:\n    import json\n")
    )


def test_control_entrypoint_dunder_main_dev_import_not_required() -> None:
    """An entrypoint is IMPORTED by uvicorn, so ``__name__ != '__main__'`` and its
    ``if __name__ == '__main__':`` block is dead on import — a dev-only ``import <absent>`` there
    must NOT be required (must not over-reject an ordinary entrypoint)."""
    _assert_candidate(
        *_pentry(
            f"from fastapi import FastAPI\napp = FastAPI()\n"
            f"if __name__ == '__main__':\n    import {_ABSENT}\n    {_ABSENT}.run(app)\n"
        )
    )


def test_control_entrypoint_called_setup_installed_import_is_candidate() -> None:
    """The E1 shape with an INSTALLED import (fastapi) proves out — the fix requires the import, and
    it resolves — candidate."""
    _assert_candidate(
        *_pentry(
            "from fastapi import FastAPI\n"
            "def _setup():\n    import fastapi\n"
            "_setup()\napp = FastAPI()\n"
        )
    )


def test_control_real_factory_flag_unchanged() -> None:
    """A genuine ``--factory`` target (``uvicorn main:create --factory``) whose zero-arg factory
    returns ``FastAPI()`` still proves out — the factory shape path is untouched."""
    files, intent = _pentry(
        "from fastapi import FastAPI\ndef create():\n    return FastAPI()\n",
        start_cmd=("uvicorn", "main:create", "--factory"),
    )
    _assert_candidate(files, intent)


def test_control_factory_body_absent_import_fails_closed() -> None:
    """A ``--factory`` whose body imports an absent pkg fails closed (target-scope import proof)."""
    files, intent = _pentry(
        f"from fastapi import FastAPI\ndef create():\n    import {_ABSENT}\n    return FastAPI()\n",
        start_cmd=("uvicorn", "main:create", "--factory"),
    )
    _assert_needs_review(files, intent, "entrypoint_unresolved", _ENTRY_DEP_MSG)


def test_factory_call_binding_stays_shape_failclosed_by_design() -> None:
    """DELIBERATE, PRE-EXISTING design (NOT a defect of this round): a module-scope FACTORY-CALL
    binding ``app = create_app()`` WITHOUT ``--factory`` has no closed server-compatible app SHAPE
    (only ``app = FastAPI()`` literal construction, an exact protocol callable, or a ``--factory``
    callable are recognized), so it fails closed as ``entrypoint_unresolved`` — see the reject
    fixture ``app = make_app()`` (with fastapi installed) in ``test_r7_python_proof.py`` and the
    ``_FASTAPI_APP_RE`` module-scope-only comment in ``detect.py``. The Round-5 import fix DOES
    prove the (installed) ``from fastapi import FastAPI`` in ``create_app`` now that it is
    unconditionally called, but the app-SHAPE verdict is orthogonal and correctly unchanged. This
    test pins that the two import defects did not silently alter the shape proof."""
    files, intent = _pentry(
        "def create_app():\n    from fastapi import FastAPI\n    return FastAPI()\n"
        "app = create_app()\n"
    )
    _assert_needs_review(files, intent, "entrypoint_unresolved", _ENTRY_SHAPE_MSG)


# ===========================================================================
# CORRECTION ROUND 6 — the CERTAIN-EVALUATION model
#
# Round 5 attributed a called local ONLY at a bare ``name()`` / ``x = name()`` statement.  Every
# OTHER position where a call is STATICALLY CERTAIN to run at module load stayed unattributed, so an
# absent import inside the callee produced a false candidate that boot-crashes.  Round 6 attributes
# called local wherever the call is certain: a guarded ``try`` body (with the guard's SWALLOW/ABORT
# tier composed in), a class body, a decorator, a default value, any argument or operand that
# evaluates before/around the call, an f-string, an eager non-empty comprehension element, a ``for``
# iterable, a ``with`` context manager, a ternary's taken arm, a ``return`` value, and the inverted
# ``__name__ != '__main__'`` ``else``.  The controls prove the boundary holds: a swallowing guard,
# a short-circuited operand, an empty/lazy comprehension, an installed import, an uncalled helper
# all stay candidate — no ordinary runnable app is over-rejected.
# ===========================================================================


def _absent_local(name: str) -> str:
    """A module-scope ``def <name>`` whose body imports the absent package (crashes if run)."""
    return f"def {name}():\n    import {_ABSENT}\n    return {_ABSENT}\n"


# --- A1: a call inside a try body whose handler does NOT catch the ImportError -----------------


def test_r6_a1_try_body_call_non_catching_handler_fails_closed() -> None:
    """A1: ``try: run() except ConnectionError: sys.exit(1)`` — the handler catches ConnectionError,
    NOT the callee's ModuleNotFoundError, so the absent import escapes and crashes. Fails closed."""
    files, intent = _pmig(
        "import sys\n"
        + _absent_local("run")
        + "try:\n    run()\nexcept ConnectionError:\n    sys.exit(1)\n"
    )
    _assert_needs_review(files, intent, "migrate_target_unresolved", _MIGRATE_IMPORT_MSG)


def test_r6_a1_try_body_call_catching_but_aborting_handler_fails_closed() -> None:
    """A catching handler that ABORTS (``except ModuleNotFoundError: sys.exit(1)``) does NOT rescue
    the app — it exits non-zero — so the tier is ABORTED (not swallowed) and it fails closed."""
    files, intent = _pmig(
        "import sys\n"
        + _absent_local("run")
        + "try:\n    run()\nexcept ModuleNotFoundError:\n    sys.exit(1)\n"
    )
    _assert_needs_review(files, intent, "migrate_target_unresolved", _MIGRATE_IMPORT_MSG)


# --- A2: class-body statement --------------------------------------------------------------------


def test_r6_a2_class_body_call_fails_closed() -> None:
    """A2: ``class Settings: engine = build()`` runs ``build()`` at class-definition time; the
    absent import inside ``build`` really executes. Must fail closed."""
    files, intent = _pmig(_absent_local("build") + "class Settings:\n    engine = build()\n")
    _assert_needs_review(files, intent, "migrate_target_unresolved", _MIGRATE_IMPORT_MSG)


def test_r6_a2_class_body_call_on_entrypoint_fails_closed() -> None:
    """A2 on the ENTRYPOINT surface — uvicorn imports the module, the class body runs on import."""
    files, intent = _pentry(
        "from fastapi import FastAPI\n"
        + _absent_local("build")
        + "class Settings:\n    engine = build()\n"
        + "app = FastAPI()\n"
    )
    _assert_needs_review(files, intent, "entrypoint_unresolved", _ENTRY_DEP_MSG)


# --- A3: decorator ------------------------------------------------------------------------------


def test_r6_a3_decorator_call_fails_closed() -> None:
    """A3: ``@register`` applies ``register(upgrade)`` at def-time; the absent import inside
    ``register`` really executes. Must fail closed."""
    files, intent = _pmig(
        f"def register(fn):\n    import {_ABSENT}\n    return fn\n"
        "@register\ndef upgrade():\n    pass\n"
    )
    _assert_needs_review(files, intent, "migrate_target_unresolved", _MIGRATE_IMPORT_MSG)


def test_r6_a3_decorator_call_on_entrypoint_fails_closed() -> None:
    """A3 on the ENTRYPOINT surface — the decorator runs when uvicorn imports the module."""
    files, intent = _pentry(
        "from fastapi import FastAPI\n"
        f"def register(fn):\n    import {_ABSENT}\n    return fn\n"
        "@register\ndef upgrade():\n    pass\n"
        "app = FastAPI()\n"
    )
    _assert_needs_review(files, intent, "entrypoint_unresolved", _ENTRY_DEP_MSG)


# --- A4: an argument (evaluated before the call) -------------------------------------------------


def test_r6_a4_argument_call_fails_closed() -> None:
    """A4: ``logging.config.dictConfig(cfg())`` evaluates ``cfg()`` (the argument) before the outer
    call, so the absent import inside ``cfg`` really executes. Must fail closed."""
    files, intent = _pmig(
        "import logging.config\n"
        f"def cfg():\n    import {_ABSENT}\n    return {{}}\n"
        "logging.config.dictConfig(cfg())\n"
    )
    _assert_needs_review(files, intent, "migrate_target_unresolved", _MIGRATE_IMPORT_MSG)


# --- A5: exotic certain-evaluation positions -----------------------------------------------------


def test_r6_a5_eager_nonempty_comprehension_element_fails_closed() -> None:
    """A5a: ``[s() for _ in [1]]`` — an eager list comprehension over a non-empty literal runs the
    element exactly once, so ``s()`` really executes. Must fail closed."""
    files, intent = _pmig(_absent_local("s") + "[s() for _ in [1]]\n")
    _assert_needs_review(files, intent, "migrate_target_unresolved", _MIGRATE_IMPORT_MSG)


def test_r6_a5_fstring_value_fails_closed() -> None:
    """A5b: an f-string ``f"{s()}"`` evaluates every formatted value, so ``s()`` really executes."""
    files, intent = _pmig(_absent_local("s") + 'x = f"{s()}"\n')
    _assert_needs_review(files, intent, "migrate_target_unresolved", _MIGRATE_IMPORT_MSG)


def test_r6_a5_boolop_first_operand_fails_closed() -> None:
    """A5c: ``s() or 1`` evaluates its FIRST operand unconditionally, so ``s()`` really executes."""
    files, intent = _pmig(_absent_local("s") + "s() or 1\n")
    _assert_needs_review(files, intent, "migrate_target_unresolved", _MIGRATE_IMPORT_MSG)


def test_r6_a5_with_context_manager_fails_closed() -> None:
    """A5d: ``with s(): ...`` evaluates the context-manager expression, so ``s()`` runs."""
    files, intent = _pmig(_absent_local("s") + "with s():\n    pass\n")
    _assert_needs_review(files, intent, "migrate_target_unresolved", _MIGRATE_IMPORT_MSG)


def test_r6_a5_ternary_taken_arm_fails_closed() -> None:
    """A5e: ``s() if True else 0`` — the statically-true test takes the ``s()`` arm, which runs."""
    files, intent = _pmig(_absent_local("s") + "s() if True else 0\n")
    _assert_needs_review(files, intent, "migrate_target_unresolved", _MIGRATE_IMPORT_MSG)


# --- A6: the inverted __main__ guard -------------------------------------------------------------


def test_r6_a6_inverted_main_guard_else_fails_closed() -> None:
    """A6: ``if __name__ != '__main__': pass`` ``else: main()`` — as a ``python <script>`` the
    module IS ``__main__`` so the ``!=`` test is False and the ``else`` runs ``main()``. Fails."""
    files, intent = _pmig(
        _absent_local("main") + "if __name__ != '__main__':\n    pass\nelse:\n    main()\n"
    )
    _assert_needs_review(files, intent, "migrate_target_unresolved", _MIGRATE_IMPORT_MSG)


# --- additional certain positions the generalized walker now covers ------------------------------


def test_r6_return_value_call_fails_closed() -> None:
    """A ``return g()`` evaluates the return value, so a called local's return-value call runs."""
    files, intent = _pmig(_absent_local("h") + "def g():\n    return h()\ng()\n")
    _assert_needs_review(files, intent, "migrate_target_unresolved", _MIGRATE_IMPORT_MSG)


def test_r6_if_test_call_fails_closed() -> None:
    """An ``if check(): ...`` always evaluates its test, so ``check()`` runs whatever the branch."""
    files, intent = _pmig(_absent_local("check") + "if check():\n    pass\n")
    _assert_needs_review(files, intent, "migrate_target_unresolved", _MIGRATE_IMPORT_MSG)


def test_r6_for_iterable_call_fails_closed() -> None:
    """A ``for x in gen(): ...`` always evaluates the iterable to obtain the iterator."""
    files, intent = _pmig(_absent_local("gen") + "for x in gen():\n    pass\n")
    _assert_needs_review(files, intent, "migrate_target_unresolved", _MIGRATE_IMPORT_MSG)


def test_r6_default_value_call_fails_closed() -> None:
    """A ``def f(x=build()): ...`` evaluates the default at def-time, so ``build()`` runs."""
    files, intent = _pmig(_absent_local("build") + "def f(x=build()):\n    return x\n")
    _assert_needs_review(files, intent, "migrate_target_unresolved", _MIGRATE_IMPORT_MSG)


def test_r6_composed_guard_threads_through_call_chain() -> None:
    """The ambient guard threads through the call chain: ``f()`` runs behind a module-scope
    ``except ImportError`` and ``f`` calls ``g()`` unguarded; ``g``'s absent import is swallowed by
    the ambient handler, so the app is candidate (an ordinary optional-dependency shape)."""
    _assert_candidate(
        *_pmig(
            _absent_local("g")
            + "def f():\n    g()\n"
            + "try:\n    f()\nexcept ImportError:\n    pass\n"
        )
    )


# --- CORRECTION ROUND 6 controls — must NOT over-reject an ordinary runnable app -----------------


def test_r6_control_import_error_guard_swallows_stays_candidate() -> None:
    """A ``try: run() except ImportError: pass`` swallows the callee's ModuleNotFoundError — an
    ordinary optional-dependency guard. Must stay candidate."""
    _assert_candidate(
        *_pmig(_absent_local("run") + "try:\n    run()\nexcept ImportError:\n    pass\n")
    )


def test_r6_control_bare_except_swallows_stays_candidate() -> None:
    """A bare ``except:`` swallows every catchable failure, so the callee's absent import is
    tolerated. Must stay candidate."""
    _assert_candidate(*_pmig(_absent_local("run") + "try:\n    run()\nexcept:\n    pass\n"))


def test_r6_control_catching_swallowing_module_handler_stays_candidate() -> None:
    """``except ModuleNotFoundError: pass`` catches AND swallows the missing module. Candidate."""
    _assert_candidate(
        *_pmig(_absent_local("run") + "try:\n    run()\nexcept ModuleNotFoundError:\n    pass\n")
    )


def test_r6_control_empty_comprehension_element_stays_candidate() -> None:
    """``[s() for _ in []]`` — the empty literal never runs the element, so ``s()`` never runs."""
    _assert_candidate(*_pmig(_absent_local("s") + "[s() for _ in []]\n"))


def test_r6_control_lazy_generator_element_stays_candidate() -> None:
    """A lazy generator ``(s() for _ in [1])`` does NOT run the element until iterated (it never is
    here), so ``s()`` is not certain. Must stay candidate."""
    _assert_candidate(*_pmig(_absent_local("s") + "g = (s() for _ in [1])\n"))


def test_r6_control_boolop_right_operand_short_circuits_stays_candidate() -> None:
    """``1 or s()`` short-circuits on the truthy left operand, so ``s()`` never runs — candidate."""
    _assert_candidate(*_pmig(_absent_local("s") + "1 or s()\n"))


def test_r6_control_ternary_not_taken_arm_stays_candidate() -> None:
    """``s() if False else 0`` takes the ``else`` arm, so ``s()`` never runs — candidate."""
    _assert_candidate(*_pmig(_absent_local("s") + "s() if False else 0\n"))


def test_r6_control_decorator_installed_import_stays_candidate() -> None:
    """A ``@register`` decorator whose body imports the INSTALLED fastapi proves out — candidate."""
    _assert_candidate(
        *_pmig(
            "def register(fn):\n    import fastapi\n    return fn\n"
            "@register\ndef upgrade():\n    pass\n"
        )
    )


def test_r6_control_class_body_installed_import_stays_candidate() -> None:
    """A class body calling a local whose body imports INSTALLED fastapi proves out — candidate."""
    _assert_candidate(
        *_pmig(
            "def build():\n    import fastapi\n    return fastapi\n"
            "class Settings:\n    engine = build()\n"
        )
    )


def test_r6_control_mutual_recursion_with_absent_import_fails_closed() -> None:
    """A direct bare call into mutual recursion ``a -> b -> a`` where ``a`` imports the absent
    package is bounded by the visited-set and still fails closed (needs_review)."""
    files, intent = _pmig(
        f"def a():\n    import {_ABSENT}\n    return b()\ndef b():\n    return a()\na()\n"
    )
    _assert_needs_review(files, intent, "migrate_target_unresolved", _MIGRATE_IMPORT_MSG)


def test_r6_control_uncalled_helper_absent_import_stays_candidate() -> None:
    """A genuinely uncalled helper with an absent import must not have its import required —
    candidate (the reachability boundary is preserved)."""
    _assert_candidate(*_pmig(_absent_local("helper") + "x = 1\n"))


def test_r6_control_class_body_shadowed_local_not_attributed_stays_candidate() -> None:
    """A class body that REBINDS ``build`` before calling it shadows the module function, so the
    absent-importing module ``build`` is not the callee — must not over-reject. Candidate."""
    _assert_candidate(
        *_pmig(
            _absent_local("build")
            + "class Settings:\n    def build():\n        return 0\n    engine = build()\n"
        )
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
