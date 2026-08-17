"""runthru-v2 ROOT-2: the agent prefixing paths with 'workspace/' (cued by prompts
that say files live in /workspace) doubled the prefix to /workspace/workspace/foo,
which blanked the live preview (server serves the ws ROOT → 404) and broke plan-step
done-conditions (they check the root). The sandbox path resolvers now strip ONE
redundant leading 'workspace/' or '/workspace/'. This guards the normalization."""

from __future__ import annotations

from pathlib import Path

from disco.tools.sandbox.base import SandboxSpec
from disco.tools.sandbox.base import strip_redundant_workspace_prefix as strip
from disco.tools.sandbox.process import ProcessSandboxInstance


def test_strip_redundant_workspace_prefix_cases():
    assert strip("workspace/index.html") == "index.html"
    assert strip("/workspace/css/app.css") == "css/app.css"
    assert strip("index.html") == "index.html"  # no prefix → untouched
    assert strip("src/workspace/x.js") == "src/workspace/x.js"  # nested → untouched
    assert strip("workspace") == ""  # the ws root itself
    assert strip("/workspace") == ""


def test_process_resolver_does_not_double_workspace(tmp_path: Path):
    inst = ProcessSandboxInstance(
        id="root2-test",
        owner_id="owner-root2",
        conversation_id="conv-root2",
        spec=SandboxSpec(),
        workspace=tmp_path,
    )
    # the agent writing "workspace/index.html" must land at the SAME place as "index.html"
    assert inst._resolve("workspace/index.html") == inst._resolve("index.html")
    assert inst._resolve("/workspace/css/app.css") == inst._resolve("css/app.css")
    assert "workspace/workspace" not in str(inst._resolve("workspace/index.html"))
