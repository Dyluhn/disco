"""Mutation tests for the generated/declarative proof gate.

Every stable proof boundary is exercised against the requested root. Complete
entries use a Git-backed generator, source, output, and exact collected test
node; regeneration writes only through the one ``{TEMP_OUTPUT}`` argv token.
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers import (  # noqa: E402
    assert_problem_contains,
    git_add,
    git_commit,
    make_temp_repo,
    write,
    write_and_track,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from architecture import generated  # noqa: E402

GENERATOR_PATH = "scripts/gen.py"
SOURCE_PATH = "inputs/source.json"
OUTPUT_PATH = "architecture/out.json"
TEST_PATH = "tests/test_gen.py"
TEST_NODE = f"{TEST_PATH}::test_gen"
OUTPUT_BYTES = b'{"kind":"proof"}\n'


def _full_entry(
    gen_path: str = GENERATOR_PATH,
    output_path: str = OUTPUT_PATH,
) -> dict[str, Any]:
    """Return a complete entry using the host's actual Python version."""
    return {
        "id": "GEN-001",
        "generator_path": gen_path,
        "regeneration_command": [
            "python",
            gen_path,
            generated.TEMP_OUTPUT_PLACEHOLDER,
        ],
        "expected_sha256": hashlib.sha256(OUTPUT_BYTES).hexdigest(),
        "content_scan_policy": {
            "family": "declarative_json",
            "required_markers": ['"kind"'],
        },
        "owner_package": "PKG-02-TEST",
        "regeneration_test": TEST_PATH,
        "output_path": output_path,
        "temp_output_placeholder": generated.TEMP_OUTPUT_PLACEHOLDER,
        "source_inputs": sorted([SOURCE_PATH, gen_path]),
        "parser_compiler_versions": {"python": platform.python_version()},
        "removal_duty": "Remove when the generated exception is retired.",
        "regeneration_test_node_id": TEST_NODE,
    }


def _command_entry(command: Any) -> dict[str, Any]:
    return {
        "id": "GEN-COMMAND",
        "generator_path": GENERATOR_PATH,
        "regeneration_command": command,
        "output_path": OUTPUT_PATH,
        "temp_output_placeholder": generated.TEMP_OUTPUT_PLACEHOLDER,
    }


def _write_registry(root: Path, entries: list[dict[str, Any]]) -> None:
    write(
        root / "architecture" / "generated.json",
        json.dumps(
            {
                "schema": "disclaude-architecture-generated-proof-v1",
                "entries": entries,
            }
        ),
    )


def _init_repo(root: Path) -> None:
    subprocess.run(
        ["git", "init", str(root)],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "test@test.test"],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "Test"],
        capture_output=True,
        check=True,
    )


def _write_complete_repo(root: Path) -> dict[str, Any]:
    """Write and commit every authority needed by a complete entry."""
    generator_source = (
        "from pathlib import Path\n"
        "import sys\n"
        f"source = Path({SOURCE_PATH!r}).read_bytes()\n"
        "Path(sys.argv[1]).write_bytes(source)\n"
    )
    write_and_track(root, GENERATOR_PATH, generator_source)
    write_and_track(root, SOURCE_PATH, OUTPUT_BYTES.decode())
    write_and_track(root, OUTPUT_PATH, OUTPUT_BYTES.decode())
    write_and_track(
        root,
        TEST_PATH,
        "from pathlib import Path\n"
        "\n"
        "def test_gen():\n"
        f"    source = Path({SOURCE_PATH!r}).read_bytes()\n"
        f"    output = Path({OUTPUT_PATH!r}).read_bytes()\n"
        "    assert output == source\n",
    )
    entry = _full_entry()
    _write_registry(root, [entry])
    git_add(root, "architecture/generated.json")
    git_commit(root)
    return entry


def _assert_missing_field(root: Path, field: str) -> None:
    entry = _full_entry()
    del entry[field]
    _write_registry(root, [entry])
    result = generated.check_generated(root)
    assert result["entry_count"] == 1
    assert_problem_contains(result["problems"], "missing proof fields", field)


class TestMissingProofFacts:
    def test_missing_generator_path_fails(self, tmp_path: Path) -> None:
        _assert_missing_field(tmp_path, "generator_path")

    def test_missing_regen_command_fails(self, tmp_path: Path) -> None:
        _assert_missing_field(tmp_path, "regeneration_command")

    def test_missing_expected_sha256_fails(self, tmp_path: Path) -> None:
        _assert_missing_field(tmp_path, "expected_sha256")

    def test_missing_content_scan_policy_fails(
        self, tmp_path: Path
    ) -> None:
        _assert_missing_field(tmp_path, "content_scan_policy")

    def test_missing_owner_fails(self, tmp_path: Path) -> None:
        _assert_missing_field(tmp_path, "owner_package")

    def test_missing_regen_test_fails(self, tmp_path: Path) -> None:
        _assert_missing_field(tmp_path, "regeneration_test")

    def test_missing_output_path_fails(self, tmp_path: Path) -> None:
        _assert_missing_field(tmp_path, "output_path")

    def test_missing_temp_output_placeholder_fails(
        self, tmp_path: Path
    ) -> None:
        _assert_missing_field(tmp_path, "temp_output_placeholder")

    def test_missing_source_inputs_fails(self, tmp_path: Path) -> None:
        _assert_missing_field(tmp_path, "source_inputs")

    def test_missing_parser_compiler_versions_fails(
        self, tmp_path: Path
    ) -> None:
        _assert_missing_field(tmp_path, "parser_compiler_versions")

    def test_missing_removal_duty_fails(self, tmp_path: Path) -> None:
        _assert_missing_field(tmp_path, "removal_duty")

    def test_missing_regen_test_node_id_fails(
        self, tmp_path: Path
    ) -> None:
        _assert_missing_field(tmp_path, "regeneration_test_node_id")


class TestRegenCommand:
    def test_shell_chaining_fails(self) -> None:
        string_command = f"python {GENERATOR_PATH} {{TEMP_OUTPUT}}"
        problem = generated._check_regen_command(
            _command_entry(string_command)
        )
        assert problem is not None
        assert "exact argv array" in problem

    def test_pipe_chaining_fails(self) -> None:
        command = [
            "python",
            GENERATOR_PATH,
            generated.TEMP_OUTPUT_PLACEHOLDER,
            "|",
            "cat",
        ]
        problem = generated._check_regen_command(_command_entry(command))
        assert problem is not None
        assert "shell syntax" in problem

    def test_disallowed_prefix_fails(self) -> None:
        bash = [
            "bash",
            GENERATOR_PATH,
            generated.TEMP_OUTPUT_PLACEHOLDER,
        ]
        problem = generated._check_regen_command(_command_entry(bash))
        assert problem is not None
        assert "must start with" in problem

        wrong_generator = [
            "python",
            "scripts/other.py",
            generated.TEMP_OUTPUT_PLACEHOLDER,
        ]
        problem = generated._check_regen_command(
            _command_entry(wrong_generator)
        )
        assert problem is not None
        assert "exact generator_path" in problem

    def test_empty_command_fails(self) -> None:
        problem = generated._check_regen_command(_command_entry([]))
        assert problem is not None
        assert "non-empty exact argv" in problem

        non_string = [
            "python",
            GENERATOR_PATH,
            generated.TEMP_OUTPUT_PLACEHOLDER,
            7,
        ]
        problem = generated._check_regen_command(
            _command_entry(non_string)
        )
        assert problem is not None
        assert "argv tokens must be strings" in problem

        no_placeholder = ["python", GENERATOR_PATH]
        problem = generated._check_temp_output_placeholder(
            _command_entry(no_placeholder)
        )
        assert problem is not None
        assert "exactly one" in problem

        duplicate = [
            "python",
            GENERATOR_PATH,
            generated.TEMP_OUTPUT_PLACEHOLDER,
            generated.TEMP_OUTPUT_PLACEHOLDER,
        ]
        problem = generated._check_temp_output_placeholder(
            _command_entry(duplicate)
        )
        assert problem is not None
        assert "exactly one" in problem

    def test_command_substitution_fails(self) -> None:
        command = [
            "python",
            GENERATOR_PATH,
            generated.TEMP_OUTPUT_PLACEHOLDER,
            "$(printf injected)",
        ]
        problem = generated._check_regen_command(_command_entry(command))
        assert problem is not None
        assert "shell syntax" in problem

        service_url = [
            "python",
            GENERATOR_PATH,
            generated.TEMP_OUTPUT_PLACEHOLDER,
            "https://service.example.test",
        ]
        problem = generated._check_regen_command(
            _command_entry(service_url)
        )
        assert problem is not None
        assert "injects a service URL" in problem

    def test_backtick_fails(self) -> None:
        command = [
            "python",
            GENERATOR_PATH,
            generated.TEMP_OUTPUT_PLACEHOLDER,
            "`injected`",
        ]
        problem = generated._check_regen_command(_command_entry(command))
        assert problem is not None
        assert "shell syntax" in problem

        branch_ref = [
            "python",
            GENERATOR_PATH,
            generated.TEMP_OUTPUT_PLACEHOLDER,
            "--ref",
            "feature/injected",
        ]
        problem = generated._check_regen_command(
            _command_entry(branch_ref)
        )
        assert problem is not None
        assert "injects a branch/ref" in problem

    def test_redirect_fails(self) -> None:
        redirect = [
            "python",
            GENERATOR_PATH,
            generated.TEMP_OUTPUT_PLACEHOLDER,
            ">",
            OUTPUT_PATH,
        ]
        problem = generated._check_regen_command(_command_entry(redirect))
        assert problem is not None
        assert "shell syntax" in problem

        live_target = [
            "python",
            GENERATOR_PATH,
            generated.TEMP_OUTPUT_PLACEHOLDER,
            OUTPUT_PATH,
        ]
        problem = generated._check_regen_command(
            _command_entry(live_target)
        )
        assert problem is not None
        assert "command targets the live output" in problem

    def test_allowed_prefixes_pass(self) -> None:
        commands = (
            [
                "python",
                GENERATOR_PATH,
                generated.TEMP_OUTPUT_PLACEHOLDER,
            ],
            [
                "uv",
                "run",
                "python",
                GENERATOR_PATH,
                generated.TEMP_OUTPUT_PLACEHOLDER,
            ],
            [
                "node",
                GENERATOR_PATH,
                generated.TEMP_OUTPUT_PLACEHOLDER,
            ],
        )
        for command in commands:
            entry = _command_entry(command)
            assert generated._check_regen_command(entry) is None
            assert generated._check_temp_output_placeholder(entry) is None


class TestTrackedGenerator:
    def test_oversized_wrapper_fails(self) -> None:
        with make_temp_repo() as temp_name:
            root = Path(temp_name)
            write_and_track(root, GENERATOR_PATH, "pass\n" * 301)
            git_commit(root)
            entry = {"id": "GEN-BAD", "generator_path": GENERATOR_PATH}
            problem = generated._check_tracked_generator(entry, root)
            assert problem is not None
            assert "oversized wrapper" in problem

    def test_untracked_generator_fails(self, tmp_path: Path) -> None:
        write(tmp_path / GENERATOR_PATH, "print('untracked')\n")
        entry = {"id": "GEN-BAD", "generator_path": GENERATOR_PATH}
        problem = generated._check_tracked_generator(entry, tmp_path)
        assert problem is not None
        assert "not Git-tracked" in problem

    def test_small_wrapper_passes(self) -> None:
        with make_temp_repo() as temp_name:
            root = Path(temp_name)
            write_and_track(root, GENERATOR_PATH, "print('small')\n")
            git_commit(root)
            entry = {"id": "GEN-OK", "generator_path": GENERATOR_PATH}
            assert generated._check_tracked_generator(entry, root) is None


class TestOwner:
    def test_missing_owner_fails(self) -> None:
        entry = {
            "id": "GEN-BAD",
            "owner_package": "",
            "removal_duty": "Remove it.",
        }
        problem = generated._check_owner(entry)
        assert problem is not None
        assert "invalid owner_package" in problem

    def test_present_owner_passes(self) -> None:
        entry = {
            "id": "GEN-OK",
            "owner_package": "PKG-02-TEST",
            "removal_duty": "Remove it.",
        }
        assert generated._check_owner(entry) is None

    def test_missing_removal_duty_fails(self) -> None:
        entry = {
            "id": "GEN-BAD",
            "owner_package": "PKG-02-TEST",
            "removal_duty": "",
        }
        problem = generated._check_removal_duty(entry)
        assert problem is not None
        assert "no removal_duty" in problem

    def test_present_removal_duty_passes(self) -> None:
        entry = {
            "id": "GEN-OK",
            "owner_package": "PKG-02-TEST",
            "removal_duty": "Delete after a handwritten replacement ships.",
        }
        assert generated._check_removal_duty(entry) is None


class TestRegenTest:
    def test_missing_regen_test_fails(self, tmp_path: Path) -> None:
        entry = {
            "id": "GEN-BAD",
            "regeneration_test": TEST_PATH,
            "regeneration_test_node_id": f"{TEST_PATH}::missing",
        }
        problem = generated._check_regen_test(entry, tmp_path)
        assert problem is not None
        assert "not Git-tracked" in problem

        problem = generated._check_regen_test_node_id(entry, tmp_path)
        assert problem is not None
        assert "not collected exactly" in problem

    def test_present_regen_test_passes(self, tmp_path: Path) -> None:
        git_root = tmp_path / "repo"
        git_root.mkdir()
        _init_repo(git_root)
        write_and_track(git_root, SOURCE_PATH, OUTPUT_BYTES.decode())
        write_and_track(git_root, OUTPUT_PATH, OUTPUT_BYTES.decode())
        write_and_track(
            git_root,
            TEST_PATH,
            "from pathlib import Path\n"
            "\n"
            "def test_gen():\n"
            f"    source = Path({SOURCE_PATH!r}).read_bytes()\n"
            f"    output = Path({OUTPUT_PATH!r}).read_bytes()\n"
            "    assert output == source\n",
        )
        entry = {
            "id": "GEN-OK",
            "regeneration_test": TEST_PATH,
            "regeneration_test_node_id": TEST_NODE,
        }
        assert generated._check_regen_test(entry, git_root) is None
        assert generated._check_regen_test_node_id(entry, git_root) is None

        entry["regeneration_test_node_id"] = f"{TEST_PATH}::test_other"
        problem = generated._check_regen_test_node_id(entry, git_root)
        assert problem is not None
        assert TEST_PATH in problem
        assert "not collected exactly" in problem


class TestContentScan:
    def test_declarative_data_valid_json_passes(
        self, tmp_path: Path
    ) -> None:
        write(tmp_path / "out.json", '{"kind":"proof","nested":{"safe":true}}')
        entry = {
            "id": "GEN-OK",
            "output_path": "out.json",
            "content_scan_policy": {
                "family": "declarative_json",
                "required_markers": ['"kind"'],
            },
        }
        assert generated._check_content_scan(entry, tmp_path) is None

        write(tmp_path / "out.json", '{"kind":"proof","nested":{"service":"x"}}')
        problem = generated._check_content_scan(entry, tmp_path)
        assert problem is not None
        assert "injection key: service" in problem

    def test_declarative_data_invalid_json_fails(
        self, tmp_path: Path
    ) -> None:
        write(tmp_path / "out.json", '{"kind":')
        entry = {
            "id": "GEN-BAD",
            "output_path": "out.json",
            "content_scan_policy": {
                "family": "declarative_json",
                "required_markers": ['"kind"'],
            },
        }
        problem = generated._check_content_scan(entry, tmp_path)
        assert problem is not None
        assert "declarative parse failed" in problem

    def test_unknown_family_fails(self, tmp_path: Path) -> None:
        write(tmp_path / "out.data", "kind: proof\n")
        entry = {
            "id": "GEN-BAD",
            "output_path": "out.data",
            "content_scan_policy": {
                "family": "unknown_family",
                "required_markers": ["kind"],
            },
        }
        problem = generated._check_content_scan(entry, tmp_path)
        assert problem is not None
        assert "unknown content family" in problem

    def test_missing_output_for_content_scan_fails(
        self, tmp_path: Path
    ) -> None:
        entry = {
            "id": "GEN-BAD",
            "output_path": "missing.json",
            "content_scan_policy": {
                "family": "declarative_json",
                "required_markers": ["kind"],
            },
        }
        problem = generated._check_content_scan(entry, tmp_path)
        assert problem is not None
        assert "output is missing" in problem

    def test_declarative_data_with_executable_injection_fails(
        self, tmp_path: Path
    ) -> None:
        yaml_path = tmp_path / "out.yaml"
        write(yaml_path, "kind: proof\nnested:\n  safe: true\n")
        yaml_entry = {
            "id": "GEN-YAML",
            "output_path": "out.yaml",
            "content_scan_policy": {
                "family": "declarative_yaml",
                "required_markers": ["kind:"],
            },
        }
        assert generated._check_content_scan(yaml_entry, tmp_path) is None
        write(yaml_path, "kind: proof\nbranch: main\n")
        problem = generated._check_content_scan(yaml_entry, tmp_path)
        assert problem is not None
        assert "injection key: branch" in problem

        python_path = tmp_path / "out.py"
        write(
            python_path,
            '"""Generated declarations."""\n'
            "SCHEMA_VERSION = -1\n"
            'STATES = ("ready", "done")\n'
            'OPTIONS = {"enabled": True, "limits": [1, 2, None]}\n'
            'TAGS = {"stable", "generated"}\n'
            "class GeneratedProof:\n"
            '    """Literal class data."""\n'
            "    value = 1\n"
            '    metadata = {"kind": "proof"}\n',
        )
        python_entry = {
            "id": "GEN-PYTHON",
            "output_path": "out.py",
            "content_scan_policy": {
                "family": "generated_python",
                "required_markers": ["GeneratedProof"],
            },
        }
        assert generated._check_content_scan(python_entry, tmp_path) is None
        write(python_path, "class GeneratedProof(:\n")
        problem = generated._check_content_scan(python_entry, tmp_path)
        assert problem is not None
        assert "generated Python parse failed" in problem
        attacks = (
            (
                '"""GeneratedProof."""\n'
                "def choose_route(flag: bool) -> str:\n"
                "    return 'privileged' if flag else 'ordinary'\n",
                "FunctionDef",
            ),
            (
                '"""GeneratedProof."""\n'
                "async def choose_route() -> str:\n"
                "    return 'ordinary'\n",
                "AsyncFunctionDef",
            ),
            (
                '"""GeneratedProof."""\n'
                "class Coordinator:\n"
                "    def coordinate(self, route: str) -> str:\n"
                "        return route\n",
                "FunctionDef",
            ),
            (
                '"""GeneratedProof."""\n'
                "class RouteStateMachine:\n"
                "    def next_state(self, state: str) -> str:\n"
                "        if state == 'ready':\n"
                "            return 'done'\n"
                "        return 'ready'\n",
                "FunctionDef",
            ),
            (
                '"""GeneratedProof."""\n'
                "os.system('touch /tmp/generated-python-attack')\n",
                "Call",
            ),
            (
                '"""GeneratedProof."""\n'
                "@register\n"
                "class GeneratedProof:\n"
                "    value = 1\n",
                "decorators",
            ),
            (
                '"""GeneratedProof."""\n'
                "class GeneratedProof(BaseProof):\n"
                "    value = 1\n",
                "bases",
            ),
            (
                '"""GeneratedProof."""\n'
                "class GeneratedProof(metaclass=ProofMeta):\n"
                "    value = 1\n",
                "class keywords",
            ),
            (
                '"""GeneratedProof."""\nHANDLER = lambda route: route\n',
                "Lambda",
            ),
            (
                '"""GeneratedProof."""\nVALUE = build_value()\n',
                "Call",
            ),
            (
                '"""GeneratedProof."""\n'
                "if ENABLED:\n"
                "    VALUE = 1\n",
                "If",
            ),
            (
                '"""GeneratedProof."""\n'
                "VALUES = [item for item in SOURCE]\n",
                "ListComp",
            ),
            (
                '"""GeneratedProof."""\n'
                "VALUES = {item for item in SOURCE}\n",
                "SetComp",
            ),
            (
                '"""GeneratedProof."""\n'
                "VALUES = {item: item for item in SOURCE}\n",
                "DictComp",
            ),
            (
                '"""GeneratedProof."""\n'
                "VALUES = (item for item in SOURCE)\n",
                "GeneratorExp",
            ),
            (
                '"""GeneratedProof."""\n'
                "VALUE = SOURCE.attribute\n",
                "Attribute",
            ),
        )
        for attack, expected in attacks:
            write(python_path, attack)
            problem = generated._check_content_scan(python_entry, tmp_path)
            assert problem is not None
            assert "declaration-only" in problem
            assert expected in problem

        markdown_path = tmp_path / "out.md"
        write(markdown_path, "# Generated Proof\n\nDeterministic output.\n")
        markdown_entry = {
            "id": "GEN-MARKDOWN",
            "output_path": "out.md",
            "content_scan_policy": {
                "family": "generated_markdown",
                "required_markers": ["# Generated Proof"],
            },
        }
        assert generated._check_content_scan(markdown_entry, tmp_path) is None
        write(markdown_path, "# Other Document\n")
        problem = generated._check_content_scan(markdown_entry, tmp_path)
        assert problem is not None
        assert "content marker is missing" in problem


class TestEmptyRegistry:
    def test_empty_registry_passes(self, tmp_path: Path) -> None:
        _write_registry(tmp_path, [])
        result = generated.check_generated(tmp_path)
        assert result == {"ok": True, "problems": [], "entry_count": 0}

        duplicate = _full_entry()
        _write_registry(tmp_path, [duplicate, duplicate.copy()])
        result = generated.check_generated(tmp_path)
        assert not result["ok"]
        assert_problem_contains(result["problems"], "IDs", "unique")
        assert_problem_contains(result["problems"], "output paths", "unique")


class TestHashDrift:
    def test_hash_drift_fails(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        hash_root = tmp_path / "hash"
        hash_root.mkdir()
        _init_repo(hash_root)
        hash_entry = _write_complete_repo(hash_root)
        write(hash_root / OUTPUT_PATH, '{"kind":"changed"}\n')
        problem = generated._check_regen_bytes(hash_entry, hash_root)
        assert problem is not None
        assert "tracked output hash drift" in problem

        bytes_root = tmp_path / "bytes"
        bytes_root.mkdir()
        _init_repo(bytes_root)
        bytes_entry = _write_complete_repo(bytes_root)
        write(bytes_root / SOURCE_PATH, '{"kind":"different"}\n')
        problem = generated._check_regen_bytes(bytes_entry, bytes_root)
        assert problem is not None
        assert "regenerated bytes drift" in problem

        live_root = tmp_path / "live"
        live_root.mkdir()
        _init_repo(live_root)
        live_entry = _write_complete_repo(live_root)
        mutating_generator = (
            "from pathlib import Path\n"
            "import sys\n"
            f"source = Path({SOURCE_PATH!r}).read_bytes()\n"
            "Path(sys.argv[1]).write_bytes(source)\n"
            f"Path({OUTPUT_PATH!r}).parent.mkdir(parents=True, exist_ok=True)\n"
            f"Path({OUTPUT_PATH!r}).write_text('mutated live output')\n"
        )
        write(live_root / GENERATOR_PATH, mutating_generator)
        problem = generated._check_regen_bytes(live_entry, live_root)
        assert problem is not None
        assert "wrote outside the temporary output" in problem
        assert (live_root / OUTPUT_PATH).read_bytes() == OUTPUT_BYTES

        copy_root = tmp_path / "copy"
        copy_root.mkdir()
        _init_repo(copy_root)
        copy_entry = _write_complete_repo(copy_root)
        copy_generator = (
            "from pathlib import Path\n"
            "import sys\n"
            f"Path(sys.argv[1]).write_bytes(Path({OUTPUT_PATH!r}).read_bytes())\n"
        )
        write(copy_root / GENERATOR_PATH, copy_generator)
        problem = generated._check_regen_bytes(copy_entry, copy_root)
        assert problem is not None
        assert "live output absent" in problem
        assert "regeneration failed" in problem

        pwd_root = tmp_path / "pwd-copy"
        pwd_root.mkdir()
        _init_repo(pwd_root)
        _write_complete_repo(pwd_root)
        pwd_copy_generator = (
            "import os\n"
            "from pathlib import Path\n"
            "import sys\n"
            f"live = Path(os.environ['PWD']) / {OUTPUT_PATH!r}\n"
            "Path(sys.argv[1]).write_bytes(live.read_bytes())\n"
        )
        write(pwd_root / GENERATOR_PATH, pwd_copy_generator)
        monkeypatch.setenv("PWD", str(pwd_root))
        result = generated.check_generated(pwd_root)
        assert not result["ok"]
        assert_problem_contains(
            result["problems"],
            "live output absent",
            "regeneration failed",
        )
        assert (pwd_root / OUTPUT_PATH).read_bytes() == OUTPUT_BYTES

    def test_missing_expected_hash_fails(self, tmp_path: Path) -> None:
        entry = _full_entry()
        entry["expected_sha256"] = ""
        problem = generated._check_regen_bytes(entry, tmp_path)
        assert problem is not None
        assert "invalid expected_sha256" in problem

    def test_missing_output_path_fails(self, tmp_path: Path) -> None:
        entry = _full_entry(output_path="")
        problem = generated._check_regen_bytes(entry, tmp_path)
        assert problem is not None
        assert "invalid output_path" in problem

        entry = _full_entry()
        write(tmp_path / OUTPUT_PATH, OUTPUT_BYTES.decode())
        problem = generated._check_regen_bytes(entry, tmp_path)
        assert problem is not None
        assert "output" in problem
        assert "not Git-tracked" in problem


class TestSourceInputs:
    def test_missing_source_inputs_fails(self, tmp_path: Path) -> None:
        entry = {
            "id": "GEN-BAD",
            "generator_path": GENERATOR_PATH,
            "output_path": OUTPUT_PATH,
            "source_inputs": [],
        }
        problem = generated._check_source_inputs_tracked(entry, tmp_path)
        assert problem is not None
        assert "non-empty sorted unique" in problem

        entry["source_inputs"] = [SOURCE_PATH]
        problem = generated._check_source_inputs_tracked(entry, tmp_path)
        assert problem is not None
        assert "source_inputs omit generator_path" in problem

        entry["source_inputs"] = sorted([GENERATOR_PATH, OUTPUT_PATH])
        problem = generated._check_source_inputs_tracked(entry, tmp_path)
        assert problem is not None
        assert "must not include the live output" in problem

    def test_untracked_source_input_fails(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        _init_repo(root)
        write_and_track(root, GENERATOR_PATH, "print('tracked')\n")
        write(root / SOURCE_PATH, "{}\n")
        entry = {
            "id": "GEN-BAD",
            "generator_path": GENERATOR_PATH,
            "source_inputs": sorted([GENERATOR_PATH, SOURCE_PATH]),
        }
        problem = generated._check_source_inputs_tracked(entry, root)
        assert problem is not None
        assert SOURCE_PATH in problem
        assert "not Git-tracked" in problem

    def test_tracked_source_input_passes(self) -> None:
        with make_temp_repo() as temp_name:
            root = Path(temp_name)
            write_and_track(root, GENERATOR_PATH, "print('tracked')\n")
            write_and_track(root, SOURCE_PATH, "{}\n")
            git_commit(root)
            entry = {
                "id": "GEN-OK",
                "generator_path": GENERATOR_PATH,
                "source_inputs": sorted([GENERATOR_PATH, SOURCE_PATH]),
            }
            assert (
                generated._check_source_inputs_tracked(entry, root) is None
            )

            entry["source_inputs"] = [GENERATOR_PATH, GENERATOR_PATH]
            problem = generated._check_source_inputs_tracked(entry, root)
            assert problem is not None
            assert "sorted unique" in problem


class TestParserCompilerVersions:
    def test_missing_versions_fails(self) -> None:
        entry = _full_entry()
        entry["parser_compiler_versions"] = {}
        problem = generated._check_parser_compiler_versions(entry)
        assert problem is not None
        assert "invalid parser/compiler versions" in problem

        entry["parser_compiler_versions"] = {"unknown-compiler": "1.0"}
        problem = generated._check_parser_compiler_versions(entry)
        assert problem is not None
        assert "lacks exact python runtime version" in problem

    def test_present_versions_passes(self, tmp_path: Path) -> None:
        entry = _full_entry()
        assert (
            entry["parser_compiler_versions"]["python"]
            == generated._actual_versions(Path.cwd())["python"]
        )
        assert generated._check_parser_compiler_versions(entry) is None

        entry["parser_compiler_versions"]["python"] = "0.0.0"
        problem = generated._check_parser_compiler_versions(entry)
        assert problem is not None
        assert "parser/compiler version drift" in problem

        typescript_package = (
            tmp_path
            / "frontend"
            / "node_modules"
            / "typescript"
            / "package.json"
        )
        write(typescript_package, json.dumps({"version": "5.9.3"}))
        actual = generated._actual_versions(tmp_path)
        assert actual["python"] == platform.python_version()
        assert actual["node"]
        assert actual["typescript"] == "5.9.3"

        runtime_entry = _full_entry(gen_path="scripts/gen.js")
        runtime_entry["regeneration_command"] = [
            "node",
            "scripts/gen.js",
            generated.TEMP_OUTPUT_PLACEHOLDER,
        ]
        runtime_entry["parser_compiler_versions"] = {
            "python": actual["python"],
            "node": actual["node"],
            "typescript": actual["typescript"],
        }
        assert (
            generated._check_parser_compiler_versions(
                runtime_entry, tmp_path
            )
            is None
        )

        runtime_entry["parser_compiler_versions"]["node"] = "0.0.0"
        problem = generated._check_parser_compiler_versions(
            runtime_entry, tmp_path
        )
        assert problem is not None
        assert "'node'" in problem

        runtime_entry["parser_compiler_versions"]["node"] = actual["node"]
        runtime_entry["parser_compiler_versions"]["typescript"] = "0.0.0"
        problem = generated._check_parser_compiler_versions(
            runtime_entry, tmp_path
        )
        assert problem is not None
        assert "'typescript'" in problem


class TestFullyRegeneratedPositive:
    def test_valid_entry_passes_all_individual_checks(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "repo"
        root.mkdir()
        _init_repo(root)
        entry = _write_complete_repo(root)
        checks = (
            generated._check_tracked_generator(entry, root),
            generated._check_regen_command(entry),
            generated._check_temp_output_placeholder(entry),
            generated._check_source_inputs_tracked(entry, root),
            generated._check_parser_compiler_versions(entry, root),
            generated._check_regen_bytes(entry, root),
            generated._check_content_scan(entry, root),
            generated._check_owner(entry),
            generated._check_removal_duty(entry),
            generated._check_regen_test(entry, root),
            generated._check_regen_test_node_id(entry, root),
        )
        assert checks == (None,) * len(checks)
        assert (root / OUTPUT_PATH).read_bytes() == OUTPUT_BYTES
        resolved_argv = generated._isolated_argv(
            entry["regeneration_command"],
            tmp_path / "generated-output",
        )
        assert Path(resolved_argv[0]).is_absolute()
        assert resolved_argv[0] != "python"
        isolated_workspace = tmp_path / "isolated-workspace"
        isolated_workspace.mkdir()
        environment = generated._isolated_environment(isolated_workspace)
        assert environment["PWD"] == str(isolated_workspace)
        assert environment["HOME"] == str(isolated_workspace)
        assert environment["PYTHONPATH"] == str(isolated_workspace)
        assert "VIRTUAL_ENV" not in environment

    def test_git_tracked_regenerated_positive(self) -> None:
        with make_temp_repo() as temp_name:
            root = Path(temp_name)
            entry = _write_complete_repo(root)
            result = generated.check_generated(root)
            assert result == {
                "ok": True,
                "problems": [],
                "entry_count": 1,
            }
            assert (
                entry["regeneration_command"].count(
                    generated.TEMP_OUTPUT_PLACEHOLDER
                )
                == 1
            )
            assert (root / OUTPUT_PATH).read_bytes() == OUTPUT_BYTES
