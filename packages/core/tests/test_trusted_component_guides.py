"""Execute shipped wiring examples with their installed files and real HTTP."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from disco.core.trusted_components.registry import TrustedComponentRegistry


@pytest.mark.parametrize("name", ["auth-kit", "rbac-kit"])
def test_shipped_guide_mounts_from_workspace_root(tmp_path: Path, name: str) -> None:
    node = shutil.which("node")
    assert node is not None, "Node 22+ required for the shipped node:sqlite components"
    registry = TrustedComponentRegistry.default()
    for component_name in ("database-kit", "auth-kit", "rbac-kit"):
        component = registry.get(component_name)
        assert component is not None
        for relpath, data in component.install_tree().items():
            target = tmp_path / "src/trusted" / component_name / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    guide = (tmp_path / "src/trusted" / name / "GUIDE.md").read_text()
    snippet = re.search(r"```js\n(.*?)\n```", guide, re.DOTALL)
    assert snippet is not None
    checks = r'''
import assert from "node:assert/strict";
import { once } from "node:events";
import { createUser } from "./src/trusted/auth-kit/core/auth.js";
import { assignRole, revokeRole } from "./src/trusted/rbac-kit/core/roles.js";
await once(server, "listening");
const base = `http://127.0.0.1:${server.address().port}`;
try {
  assert.equal((await fetch(base + "/")).status, 200);
  assert.equal((await fetch(base + "/admin")).status, 401);
  const user = createUser(db, "guide@test.invalid", "isolated-guide-test-password");
  const response = await fetch(base + "/auth/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: user.email, password: "isolated-guide-test-password" }),
  });
  assert.equal(response.status, 200);
  const cookie = response.headers.get("set-cookie").split(";")[0];
  assert.equal((await fetch(base + "/auth/session", { headers: { cookie } })).status, 200);
  if (hasRbac) {
    const forged = await fetch(base + "/admin", { headers: { cookie, "x-role": "admin" } });
    assert.equal(forged.status, 403);
    assignRole(db, user.id, "admin");
    assert.equal((await fetch(base + "/admin", { headers: { cookie } })).status, 200);
    revokeRole(db, user.id, "admin");
    assert.equal((await fetch(base + "/admin", { headers: { cookie } })).status, 403);
  } else {
    assert.equal((await fetch(base + "/admin", { headers: { cookie } })).status, 200);
  }
  const logout = await fetch(base + "/auth/logout", { method: "POST", headers: { cookie } });
  assert.equal(logout.status, 200);
  assert.equal((await fetch(base + "/admin", { headers: { cookie } })).status, 401);
} finally {
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
  db.close();
}
'''
    program = (
        'const port = 0; const yourAppHandler = (_req, res) => res.end("ok");\n'
        f"const hasRbac = {json.dumps(name == 'rbac-kit')};\n"
        + snippet.group(1)
        + "\n"
        + checks
    )
    (tmp_path / "package.json").write_text('{"type":"module"}')
    (tmp_path / "server.mjs").write_text(program)
    result = subprocess.run(
        [node, "server.mjs"], cwd=tmp_path, capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stdout + result.stderr
