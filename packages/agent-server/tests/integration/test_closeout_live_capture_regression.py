"""Owner-adjudicated A (2026-07-17): the ``record=False`` capture exemption is
load-bearing AND strictly narrow.

The frozen live lane's §12.9 sweep asserts the runtime secret is absent from
``ComposeBundle.captured_output``. The deliberate ``exec_env`` secret-presence
probe prints the secret BY DESIGN and is therefore excluded from recording; every
ordinary Compose lifecycle surface must remain recorded and swept. These
regressions prove both directions against the REAL ``docker compose`` binary (no
mocks — the anti-bypass reading forbids them), so they carry the ``integration``
marker and run on the Docker host alongside the frozen lane. They are NOT part of
the frozen seven-case §12 selection (no ``export_track1_closeout`` marker).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ._closeout_live_support import (
    SECRET_ENV_SENTINEL,
    ComposeBundle,
    assert_no_host_coupling,
    assert_sentinel_absent,
    ingress_service_id,
    require_live_runtime,
)

pytestmark = [pytest.mark.integration]


def _sentinel_bundle(tmp_path: Path) -> ComposeBundle:
    """A minimal synthetic bundle whose ``docker compose config`` output — an
    ORDINARY, always-recorded lifecycle surface — contains the sentinel (as a
    literal default value in the compose file). No container is ever started."""
    compose_yaml = tmp_path / "compose.yaml"
    compose_yaml.write_text(
        "services:\n"
        "  web:\n"
        '    image: "busybox:1.37"\n'
        "    environment:\n"
        f'      LEAKED: "{SECRET_ENV_SENTINEL}"\n'
    )
    return ComposeBundle(project="capture-regression", bundle_dir=tmp_path)


def test_ordinary_recorded_compose_output_with_sentinel_still_fails_the_sweep(
    tmp_path: Path,
) -> None:
    """A sentinel that reaches ANY ordinary (recorded) Compose surface must still
    fail the §12.9 sweep — the exemption cannot have widened the blind spot."""
    require_live_runtime()
    bundle = _sentinel_bundle(tmp_path)
    proc = bundle.compose("config")  # ordinary lifecycle call: recorded by default
    assert proc.returncode == 0, proc.stderr
    assert SECRET_ENV_SENTINEL in proc.stdout  # the surface REALLY carries it
    with pytest.raises(AssertionError):
        assert_sentinel_absent(
            SECRET_ENV_SENTINEL,
            [("captured compose output", "".join(bundle.captured_output).encode("utf-8"))],
        )


def test_record_false_is_excluded_and_default_recording_is_unchanged(
    tmp_path: Path,
) -> None:
    """The exemption is explicit and narrow: an unrecorded call leaves
    ``captured_output`` untouched, and the very same invocation WITH the default
    records both streams."""
    require_live_runtime()
    bundle = _sentinel_bundle(tmp_path)
    unrecorded = bundle.compose("config", record=False)
    assert unrecorded.returncode == 0, unrecorded.stderr
    assert bundle.captured_output == []
    recorded = bundle.compose("config")
    assert recorded.returncode == 0, recorded.stderr
    assert len(bundle.captured_output) == 2
    assert SECRET_ENV_SENTINEL in "".join(bundle.captured_output)


# ---------------------------------------------------------------------------
# Ruling E regressions (owner-adjudicated 2026-07-17): the topology rendering is
# STRUCTURAL (no external env resolution), stays recorded, and stays swept.
# ---------------------------------------------------------------------------


def _external_env_bundle(tmp_path: Path) -> ComposeBundle:
    """A bundle whose secret arrives ONLY through the runner's external ``.env``
    (the frozen fixtures' shape): compose.yaml references ``${LEAKED:?}``."""
    (tmp_path / "compose.yaml").write_text(
        "services:\n"
        "  web:\n"
        '    image: "busybox:1.37"\n'
        "    environment:\n"
        '      LEAKED: "${LEAKED:?Set LEAKED}"\n'
        "    ports:\n"
        '      - "127.0.0.1:45999:8080"\n'
    )
    (tmp_path / ".env").write_text(f"LEAKED={SECRET_ENV_SENTINEL}\n")
    return ComposeBundle(project="capture-regression-e", bundle_dir=tmp_path)


def test_external_env_secret_is_absent_from_the_structural_rendering(
    tmp_path: Path,
) -> None:
    """Ruling E #1: a sentinel supplied only through the external ``.env`` does not
    appear in the structural JSON rendering — which stays RECORDED and SWEPT."""
    require_live_runtime()
    bundle = _external_env_bundle(tmp_path)
    doc = bundle.config_json()
    assert isinstance(doc, dict)
    assert SECRET_ENV_SENTINEL not in "".join(bundle.captured_output)
    assert_sentinel_absent(
        SECRET_ENV_SENTINEL,
        [("captured compose output", "".join(bundle.captured_output).encode("utf-8"))],
    )


def test_structural_rendering_supports_the_topology_assertions(tmp_path: Path) -> None:
    """Ruling E #2: the structural rendering is valid JSON and satisfies the exact
    topology assertions the lane relies on (ingress discovery + host-coupling scan),
    including a named volume normalized to a typed mount."""
    require_live_runtime()
    (tmp_path / "Dockerfile").write_text('FROM busybox:1.37\nCMD ["httpd", "-f"]\n')
    (tmp_path / "compose.yaml").write_text(
        "services:\n"
        "  web:\n"
        "    build:\n"
        '      context: "."\n'
        "    environment:\n"
        '      TOKEN: "${TOKEN:?Set TOKEN}"\n'
        "    ports:\n"
        '      - "127.0.0.1:45998:8080"\n'
        "    volumes:\n"
        '      - "state:/data"\n'
        "volumes:\n"
        "  state: {}\n"
    )
    (tmp_path / ".env").write_text(f"TOKEN={SECRET_ENV_SENTINEL}\n")
    bundle = ComposeBundle(project="capture-regression-e2", bundle_dir=tmp_path)
    doc = bundle.config_json()
    assert_no_host_coupling(doc)
    assert ingress_service_id(doc) == "web"
    assert SECRET_ENV_SENTINEL not in "".join(bundle.captured_output)


def test_secret_embedded_in_compose_yaml_still_fails_the_sweep(tmp_path: Path) -> None:
    """Ruling E #3: a sentinel LITERALLY embedded in compose.yaml (a real product
    leak, not runner-supplied env) still appears in the recorded structural
    rendering and still fails the sweep."""
    require_live_runtime()
    bundle = _sentinel_bundle(tmp_path)  # sentinel is a literal default in the yaml
    doc = bundle.config_json()
    assert isinstance(doc, dict)
    assert SECRET_ENV_SENTINEL in "".join(bundle.captured_output)
    with pytest.raises(AssertionError):
        assert_sentinel_absent(
            SECRET_ENV_SENTINEL,
            [("captured compose output", "".join(bundle.captured_output).encode("utf-8"))],
        )


def test_exec_env_is_the_sole_record_false_caller() -> None:
    """Ruling E #4: ``record=False`` has exactly ONE call site in the live support
    module, and it is inside ``exec_env`` — proven on the AST, not by grep."""
    import ast
    import inspect

    from . import _closeout_live_support as sup

    tree = ast.parse(inspect.getsource(sup))
    callers: list[str] = []

    class _V(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_Call(self, node: ast.Call) -> None:
            for kw in node.keywords:
                is_false = isinstance(kw.value, ast.Constant) and kw.value.value is False
                if kw.arg == "record" and is_false:
                    callers.append(self.stack[-1] if self.stack else "<module>")
            self.generic_visit(node)

    _V().visit(tree)
    assert callers == ["exec_env"], f"record=False call sites: {callers}"
