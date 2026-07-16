"""Round-5: the public release_declare tool uses the shared effective-start parser."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest
from disco.core.release.command_grammar import check_declaration_argv
from disco.core.release.spec import ReleaseIntent
from disco.tools.anatomy import ToolContext
from disco.tools.builtin.release_declare import ReleaseDeclareArgs, ReleaseDeclareTool
from disco.tools.secrets import CapabilityBroker


def test_generic_build_grammar_keeps_declared_credential_config_reference_positive() -> None:
    """Closed start semantics do not weaken the credential grammar's safe reference form.

    Package-manager config is valid install/build syntax, but never a long-running start.
    """
    check_declaration_argv(
        ("npm", "config", "set", "//registry.example/:_authToken", "${NPM_TOKEN}"),
        declared_names=frozenset({"NPM_TOKEN"}),
        field="build_cmd",
    )


def _context(
    writer: Callable[[str, str, ReleaseIntent], Awaitable[None]],
) -> ToolContext:
    return ToolContext(
        sandbox=None,
        workspace_path=".",
        timeout_s=30,
        capabilities=CapabilityBroker().grant(frozenset()),
        owner_id="owner",
        conversation_id="r7-round5",
        release_intent_writer=writer,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "start_cmd",
    [
        ["node", "server.js"],
        ["npm", "start"],
        ["uvicorn", "main:app"],
        ["python3", "-m", "gunicorn", "wsgi:app"],
        ["hypercorn", "asgi:app", "--bind", "0.0.0.0:${PORT}"],
    ],
)
async def test_tool_accepts_shared_effective_start_shapes(start_cmd: list[str]) -> None:
    written: list[ReleaseIntent] = []

    async def writer(_conversation: str, _owner: str, intent: ReleaseIntent) -> None:
        written.append(intent)

    outcome = await ReleaseDeclareTool().run(
        ReleaseDeclareArgs(start_cmd=start_cmd), _context(writer)
    )
    assert outcome.success, outcome.content
    assert len(written) == 1 and list(written[0].start_cmd) == start_cmd


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "start_cmd",
    [
        ["/usr/bin/uvicorn", "main:app"],
        ["python3.13", "-m", "uvicorn", "main:app"],
        ["python", "app.py"],
        ["npx", "serve"],
        ["node", "--require", "hook.js", "server.js"],
        ["npm", "run", "serve"],
        ["uvicorn", "main:app", "--mystery", "${PORT}"],
        ["gunicorn", "wsgi:app", "-b", "0.0.0.0:${PORT}", "--bind", "0.0.0.0:${PORT}"],
    ],
)
async def test_tool_rejects_every_shape_the_shared_parser_cannot_prove(
    start_cmd: list[str],
) -> None:
    written: list[ReleaseIntent] = []

    async def writer(_conversation: str, _owner: str, intent: ReleaseIntent) -> None:
        written.append(intent)

    outcome = await ReleaseDeclareTool().run(
        ReleaseDeclareArgs(start_cmd=start_cmd), _context(writer)
    )
    assert not outcome.success and outcome.error == "invalid_release_intent"
    assert written == []
