"""Test doubles for `test_verify_appkit_app.py`.

Extracted verbatim from that module during its PY-0889 module-size
decomposition (Epic 11-C). These are fakes and builders, not tests: no test
function moved, no assertion changed, and the collected node-id set is
unchanged. Imported as a bare top-level module, matching the established
sibling-support idiom (`_preview_manager_fakes`, `_workspace_commit_fakes`,
`_appkit_cloudflare_support`).
"""

import json
import shlex

from disco.core.appkit import (
    default_lead_gen_app_spec,
    ensure_lead_entity,
    generate,
    get_recipe,
    resolve_lead_entity,
    serialize_app_spec,
    serialize_design_spec,
)
from disco.tools.anatomy import ToolContext

# ---- build a real generated app ------------------------------------------------


def _build_tree() -> tuple[dict[str, bytes], object]:
    recipe = get_recipe("editorial-ledger")
    app = ensure_lead_entity(default_lead_gen_app_spec("Acme Leads", recipe))
    design = recipe.to_design_spec()
    tree = {p: c.encode("utf-8") for p, c in generate(app, design).items()}
    tree[".disco/appspec.json"] = serialize_app_spec(app).encode("utf-8")
    tree[".disco/designspec.json"] = serialize_design_spec(design).encode("utf-8")
    return tree, app


def _lead():
    _, app = _build_tree()
    return resolve_lead_entity(app)


# ---- fake sandbox --------------------------------------------------------------


class _ExecRes:
    def __init__(self, stdout: str, exit_code: int = 0):
        self.stdout = stdout
        self.exit_code = exit_code
        self.stderr = ""
        self.timed_out = False


class FakeSandbox:
    """In-memory workspace. `exec_shell` answers the design_lint `wc -c` size probe
    and the verify_web_app http probe; file IO is dict-backed."""

    def __init__(self, files: dict[str, bytes]):
        self._files = dict(files)

    async def file_exists(self, path: str) -> bool:
        return path in self._files

    async def read_file(self, path: str) -> bytes:
        return self._files[path]

    async def write_file(self, path: str, data: bytes) -> None:
        self._files[path] = data

    async def list_dir(self, rel: str) -> list[str]:
        rel = "" if rel in (".", "", "./") else rel.rstrip("/")
        prefix = "" if rel == "" else rel + "/"
        children: set[str] = set()
        matched = False
        for path in self._files:
            if not path.startswith(prefix):
                continue
            matched = True
            remainder = path[len(prefix) :]
            children.add(remainder.split("/", 1)[0] if "/" in remainder else remainder)
        if not matched and rel != "":
            raise FileNotFoundError(rel)  # a leaf file probed as a dir → not a dir
        return sorted(children)

    async def exec_shell(self, cmd: str, timeout_s=None):
        if cmd.startswith("wc -c <"):
            path = shlex.split(cmd)[3]  # wc -c < <path>
            data = self._files.get(path)
            if data is None:
                return _ExecRes("", exit_code=1)
            return _ExecRes(str(len(data)))
        if "import json, urllib.request as U" in cmd:
            return _ExecRes(
                json.dumps(
                    {
                        "status": 200,
                        "content_type": "text/html",
                        "body": '<script type="module" src="/assets/index-abcd1234.js"></script>',
                    }
                )
            )
        if cmd == "npm run build":
            self._files["dist/index.html"] = (
                b'<script type="module" src="/assets/index-test.js"></script>'
            )
            return _ExecRes("built")
        # the verify_web_app http probe (python3 -c ...) → server up
        return _ExecRes("200")


class BuildTrackingSandbox(FakeSandbox):
    def __init__(
        self,
        files: dict[str, bytes],
        *,
        served_index: str | None = None,
        fail_build: bool = False,
    ):
        super().__init__(files)
        self.served_index = served_index
        self.fail_build = fail_build
        self.commands: list[str] = []

    async def exec_shell(self, cmd: str, timeout_s=None):
        self.commands.append(cmd)
        if "import json, urllib.request as U" in cmd and self.served_index is not None:
            return _ExecRes(
                json.dumps(
                    {
                        "status": 200,
                        "content_type": "text/html",
                        "body": self.served_index,
                    }
                )
            )
        if cmd in ("npm ci --no-audit --no-fund", "npm install --no-audit --no-fund"):
            return _ExecRes("installed")
        if cmd == "npm run build":
            if self.fail_build:
                res = _ExecRes("", exit_code=1)
                res.stderr = "vite build failed\nsrc/App.tsx: boom"
                return res
            self._files["dist/index.html"] = (
                b'<script type="module" src="/assets/index-test.js"></script>'
            )
            return _ExecRes("built")
        return await super().exec_shell(cmd, timeout_s=timeout_s)


class MutatingSealSandbox(FakeSandbox):
    def __init__(self, files: dict[str, bytes]):
        super().__init__(files)
        self._mutate_sealed_file = False

    async def exec_shell(self, cmd: str, timeout_s=None):
        result = await super().exec_shell(cmd, timeout_s=timeout_s)
        if cmd.startswith("wc -c <") and "dist/" in cmd:
            self._mutate_sealed_file = True
        return result

    async def read_file(self, path: str) -> bytes:
        if self._mutate_sealed_file and path.startswith("dist/"):
            self._mutate_sealed_file = False
            self._files[path] = self._files[path] + b"\nchanged-during-seal"
        return await super().read_file(path)


class FileListingSandbox(FakeSandbox):
    """Match container `ls` behavior: listing a file succeeds with its path."""

    async def list_dir(self, rel: str) -> list[str]:
        if rel in self._files:
            return [f"/workspace/{rel}"]
        return await super().list_dir(rel)


class _PreviewStatus:
    def __init__(self, value: str):
        self.value = value


class BuiltPreviewManager:
    def __init__(self, *, port: int = 9134, prestarted: bool = False):
        self.port = port
        self.starts: list[dict] = []
        self.stops: list[str] = []
        self.session = None
        if prestarted:
            self.session = self._session(name="appkit-live-vite")

    def _session(self, *, name: str):
        port = self.port
        intent = {
            "serve_dir": None,
            "command": None,
            "framework": "vite",
            "cwd": None,
            "launch_kind": "framework",
        }
        return type(
            "PreviewSession",
            (),
            {
                "name": name,
                "status": _PreviewStatus("running"),
                "port": port,
                "detail": "",
                "intent": intent,
                "to_dict": lambda _self: {
                    "name": name,
                    "status": "running",
                    "port": port,
                    "projection_id": "pv_" + "a" * 32,
                    "intent": intent,
                    "launch_kind": "framework",
                },
            },
        )()

    async def start(self, **kwargs):
        self.starts.append(kwargs)
        self.session = self._session(name=kwargs.get("name") or "preview")
        return self.session

    def canonical_session(self):
        return self.session

    async def stop(self, name: str):
        self.stops.append(name)


def _ctx(sandbox, *, managed: bool = True) -> ToolContext:
    if managed and getattr(sandbox, "_preview_manager", None) is None:
        sandbox._preview_manager = BuiltPreviewManager(prestarted=True)
    return ToolContext(
        sandbox=sandbox,
        workspace_path="/workspace",
        timeout_s=30,
        capabilities=None,
        owner_id="o",
        conversation_id="c",
    )
