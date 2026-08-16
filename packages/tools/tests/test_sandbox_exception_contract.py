"""W1 — sandbox exception CONTRACT (the silent-hang root cause).

A missing-file sandbox read MUST satisfy ``isinstance(e, FileNotFoundError)`` and no
*untyped* ``SandboxError`` may leak past the read boundary. The container/podman backends
ran ``cat`` and wrapped a missing file in a plain ``SandboxError`` (subclass of Exception,
NOT FileNotFoundError) — which silently defeated ~8 ``except (FileNotFoundError, OSError)``
handlers across the loop, so the exception escaped, killed the loop task, and left the build
status RUNNING forever. The local (process) backend already raised native FileNotFoundError;
this contract makes every backend agree.
"""

import tempfile
from pathlib import Path

import pytest
from disco.tools.sandbox._container import ContainerInstance
from disco.tools.sandbox._container_parts.guest_scripts import (
    bounded_delete_result,
    bounded_read_result,
)
from disco.tools.sandbox.base import (
    SandboxError,
    SandboxFileNotFoundError,
    SandboxSpec,
    looks_like_not_found,
    raise_read_error,
)

pytestmark = pytest.mark.boundary_contract


def test_sandbox_file_not_found_is_both_types():
    """Multiple inheritance is real and instantiable (codex caveat: prove it)."""
    e = SandboxFileNotFoundError("read_file 'x': cat: /workspace/x: No such file")
    assert isinstance(e, FileNotFoundError)  # the 8 handlers catch it
    assert isinstance(e, OSError)  # FileNotFoundError IS an OSError
    assert isinstance(e, SandboxError)  # still a SandboxError for the executor's map


@pytest.mark.parametrize(
    "stderr,expected",
    [
        (b"cat: /workspace/style.css: No such file or directory", True),
        (b"cat: cannot open 'x' for reading: No such file or directory", True),
        ("No such file or directory", True),
        (b"/bin/sh: 1: cat: not found", False),  # the TOOL is missing, not the file
        (b"permission denied", False),
        (b"some unrelated failure", False),
        (b"", False),
    ],
)
def test_looks_like_not_found(stderr, expected):
    assert looks_like_not_found(stderr) is expected


def test_raise_read_error_types_by_stderr():
    """The shared boundary maps each errno-class stderr onto the matching NATIVE OSError
    subtype, so the loop's `except (FileNotFoundError, OSError)` handlers catch them all —
    and NEVER lets a bare/untyped escape happen."""
    from disco.tools.sandbox.base import SandboxNotADirectoryError, SandboxPermissionError

    with pytest.raises(SandboxFileNotFoundError):
        raise_read_error("style.css", b"cat: /workspace/style.css: No such file or directory")

    # permission denied → PermissionError (still an OSError + SandboxError)
    with pytest.raises(SandboxPermissionError) as ep:
        raise_read_error("x", b"cat: x: Permission denied")
    assert isinstance(ep.value, PermissionError)
    assert isinstance(ep.value, OSError)
    assert isinstance(ep.value, SandboxError)
    assert not isinstance(ep.value, FileNotFoundError)

    # EISDIR (cat <dir>) → IsADirectoryError
    from disco.tools.sandbox.base import SandboxIsADirectoryError

    with pytest.raises(SandboxIsADirectoryError) as ei2:
        raise_read_error("d", b"cat: d: Is a directory")
    assert isinstance(ei2.value, IsADirectoryError)
    assert isinstance(ei2.value, OSError)

    # ENOTDIR → NotADirectoryError
    with pytest.raises(SandboxNotADirectoryError) as ed:
        raise_read_error("a/b", b"cat: a/b: Not a directory")
    assert isinstance(ed.value, NotADirectoryError)
    assert isinstance(ed.value, OSError)

    # an unrecognised failure stays a plain SandboxError (never a bare untyped escape)
    with pytest.raises(SandboxError) as eu:
        raise_read_error("x", b"some unknown backend failure")
    assert not isinstance(eu.value, OSError)


async def test_process_backend_missing_file_is_native_filenotfound():
    """The local backend already raises native FileNotFoundError — lock it in."""
    from disco.tools.sandbox.base import SandboxSpec
    from disco.tools.sandbox.process import ProcessSandboxInstance

    with tempfile.TemporaryDirectory() as d:
        sbx = ProcessSandboxInstance(
            id="t", owner_id="local", conversation_id="c", spec=SandboxSpec(), workspace=Path(d)
        )
        with pytest.raises(FileNotFoundError):
            await sbx.read_file("does-not-exist.txt")


async def test_process_backend_path_escape_is_permission_error():
    """codex round-6: a path escaping the workspace jail is a TYPED PermissionError, so the
    loop's bookkeeping handlers catch it instead of a bare SandboxError crashing the task."""
    from disco.tools.sandbox.base import SandboxPermissionError, SandboxSpec
    from disco.tools.sandbox.process import ProcessSandboxInstance

    with tempfile.TemporaryDirectory() as d:
        sbx = ProcessSandboxInstance(
            id="t", owner_id="local", conversation_id="c", spec=SandboxSpec(), workspace=Path(d)
        )
        with pytest.raises(PermissionError):
            await sbx.read_file("../../etc/passwd")
        with pytest.raises(SandboxPermissionError):
            await sbx.read_file("../../etc/passwd")


def test_bounded_container_read_preserves_missing_file_type():
    """The guest protocol parser executes under ``ContainerInstance._guarded``.

    Its expected missing-file result must therefore also be a ``SandboxError``;
    otherwise the guard mistakes it for a container-client failure and turns a
    normal first write into an opaque ``sandbox op failed`` result.
    """
    with pytest.raises(SandboxFileNotFoundError) as caught:
        bounded_read_result(
            "index.html",
            44,
            b"",
            b"DISCO_READ_MISSING:[Errno 2] No such file or directory: 'index.html'",
        )
    assert isinstance(caught.value, FileNotFoundError)
    assert isinstance(caught.value, SandboxError)

    with pytest.raises(SandboxFileNotFoundError) as deleted:
        bounded_delete_result(
            "index.html",
            44,
            b"DISCO_DELETE_MISSING:[Errno 2] No such file or directory: 'index.html'",
        )
    assert isinstance(deleted.value, FileNotFoundError)
    assert isinstance(deleted.value, SandboxError)


async def test_container_guard_does_not_reclassify_missing_file_as_backend_failure():
    class MissingFileContainer:
        status = "running"

        def exec_run(self, argv, *, demux=False, workdir=None):
            del demux, workdir
            if argv[:3] == ["realpath", "-m", "--"]:
                return (0, (f"{argv[3]}\n".encode(), b""))
            return (
                44,
                (b"", b"DISCO_READ_MISSING:[Errno 2] No such file or directory: 'index.html'"),
            )

        def reload(self) -> None:
            return None

    instance = ContainerInstance(
        id="missing-file-contract",
        owner_id="local",
        conversation_id="missing-file-contract",
        spec=SandboxSpec(),
        container=MissingFileContainer(),
        container_workspace="/workspace",
        stop_timeout_s=5,
    )

    with pytest.raises(SandboxFileNotFoundError) as caught:
        await instance.read_file("index.html")
    assert str(caught.value) == "read_file 'index.html': file does not exist"
