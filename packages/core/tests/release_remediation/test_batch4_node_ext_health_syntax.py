"""Batch-4 static-detection hardening: three PROVEN defects and their over-reject controls.

Every reproduced adversarial case and every ordinary-app positive control below is a
permanent regression. The end-to-end rows drive the real ``detect_release`` (no mocks); the
lexical rows exercise the exact ``source_proof`` primitives the entry proof depends on.

Defects closed here:

* EXT-01 — a direct ``node <file>`` start whose entry extension is not a Node-executable
  module (``.js``/``.cjs``/``.mjs``) is not runnable on the neutral node image; it fails
  closed to ``entrypoint_unresolved`` instead of certifying a transpile-required entry.
* HEALTH-01 — a bare ``createServer(...)`` catch-all no longer PROVES a declared conventional
  NON-ROOT health path. Certification now requires an explicit handler for the exact path (a
  route-method registration or a ``req.url`` match for Node; a route-adjacent literal for a
  Python framework). A stray literal / framework instance without the route fails closed.
  (This is the same contract the closeout C3 ``health_no_route`` / ``comment_decoy_health``
  negatives assert; the "genuine inline catch-all certifies /healthz" reading is rejected as
  UNSOUND — it 404s under ``createServer(app)`` and contradicts both oracles.)
* SYNTAX-01/02 — a declaration with no binding identifier / an assignment with an empty RHS is
  a boot SyntaxError; it fails closed even when concatenated with a real listener, WITHOUT
  disturbing the string/template/regex/comment lexical mask (LEX controls stay unproved via the
  port proof, never via a wrong syntax rejection).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from disco.core.release.detect import Provenance, detect_release
from disco.core.release.source_proof import _invalid_bounded_js, js_executable_view
from disco.core.release.spec import ReleaseAssessment, ReleaseIntent, RuntimeStrategy

# A dependency-free public Node server: an inline unconditional catch-all responder.
NODE_SERVER = (
    "const http = require('http');\n"
    "const port = process.env.PORT;\n"
    "http.createServer((_req, res) => res.end('ok')).listen(port, '0.0.0.0');\n"
)


def _node_intent(**updates: Any) -> ReleaseIntent:
    data: dict[str, Any] = {"runtime": RuntimeStrategy.node, "start_cmd": ("node", "server.js")}
    data.update(updates)
    return ReleaseIntent(**data)


def _assess(files: dict[str, str], intent: ReleaseIntent) -> tuple[ReleaseAssessment, set[str]]:
    result = detect_release(files, intent=intent, provenance=Provenance())
    return result.assessment, {blocker.code for blocker in result.blockers}


# ---------------------------------------------------------------------------------------
# DEFECT 1 — EXT-01: only a Node-executable extension is a runnable direct-node entry.
# ---------------------------------------------------------------------------------------

_RUNNABLE_MJS = (
    "import http from 'http';\n"
    "http.createServer((_q, s) => s.end('ok')).listen(process.env.PORT, '0.0.0.0');\n"
)
_TS_BODY = (
    "const http = require('http');\n"
    "http.createServer((_q, s) => s.end('ok')).listen(process.env.PORT, '0.0.0.0');\n"
)


@pytest.mark.parametrize(
    ("entry", "source"),
    [
        ("server.js", NODE_SERVER),
        ("server.cjs", NODE_SERVER),
        ("server.mjs", _RUNNABLE_MJS),
    ],
)
def test_ext_executable_node_entry_is_candidate(entry: str, source: str) -> None:
    assessment, _ = _assess({entry: source}, _node_intent(start_cmd=("node", entry)))
    assert assessment is ReleaseAssessment.candidate


@pytest.mark.parametrize("entry", ["server.tsx", "server.ts", "server.jsx", "server.mts"])
def test_ext_transpile_required_node_entry_fails_closed(entry: str) -> None:
    assessment, codes = _assess({entry: _TS_BODY}, _node_intent(start_cmd=("node", entry)))
    assert assessment is ReleaseAssessment.needs_review
    assert "entrypoint_unresolved" in codes


def test_ext_gate_covers_npm_start_resolution() -> None:
    """The gate resolves through ``npm start`` → ``node <file>`` too, not just direct node."""
    files = {
        "server.tsx": _TS_BODY,
        "package.json": json.dumps({"scripts": {"start": "node server.tsx"}}),
    }
    assessment, codes = _assess(files, _node_intent(start_cmd=("npm", "start")))
    assert assessment is ReleaseAssessment.needs_review
    assert "entrypoint_unresolved" in codes


# ---------------------------------------------------------------------------------------
# DEFECT 2 — HEALTH-01: a declared conventional NON-ROOT health path must be PROVEN served.
# ---------------------------------------------------------------------------------------

_EXPRESS_HEALTHZ = (
    "const express = require('express');\n"
    "const app = express();\n"
    "app.get('/healthz', (q, s) => s.send('ok'));\n"
    "app.listen(process.env.PORT, '0.0.0.0');\n"
)
_REQ_URL_EQ = (
    "const http = require('http');\n"
    "const server = http.createServer((req, res) => {\n"
    "  res.end(req.url === '/healthz' ? 'ok' : 'x');\n"
    "});\n"
    "server.listen(process.env.PORT, '0.0.0.0');\n"
)
_REQ_URL_STARTSWITH = (
    "const http = require('http');\n"
    "const server = http.createServer((req, res) => {\n"
    "  res.end(req.url.startsWith('/healthz') ? 'ok' : 'x');\n"
    "});\n"
    "server.listen(process.env.PORT, '0.0.0.0');\n"
)
_CREATE_SERVER_APP = (
    "const express = require('express');\n"
    "const app = express();\n"
    "app.get('/', (q, s) => s.send('ok'));\n"
    "const http = require('http');\n"
    "http.createServer(app).listen(process.env.PORT, '0.0.0.0');\n"
)


def test_health_node_catchall_does_not_prove_nonroot_path() -> None:
    # HEALTH-01: an inline catch-all + a stray docs literal of the path is NOT a proof.
    files = {"server.js": NODE_SERVER, "docs/example.js": "const example = '/healthz';\n"}
    assessment, codes = _assess(files, _node_intent(health_path="/healthz"))
    assert assessment is ReleaseAssessment.needs_review
    assert "health_path_unresolved" in codes


def test_health_node_createserver_identifier_without_route_fails_closed() -> None:
    files = {
        "server.js": _CREATE_SERVER_APP,
        "package.json": json.dumps({"dependencies": {"express": "4.21.2"}}),
    }
    assessment, codes = _assess(files, _node_intent(health_path="/healthz"))
    assert assessment is ReleaseAssessment.needs_review
    assert "health_path_unresolved" in codes


@pytest.mark.parametrize(
    ("source", "package_json"),
    [
        (_EXPRESS_HEALTHZ, json.dumps({"dependencies": {"express": "4.21.2"}})),
        (_REQ_URL_EQ, None),
        (_REQ_URL_STARTSWITH, None),
    ],
)
def test_health_node_explicit_handler_is_candidate(source: str, package_json: str | None) -> None:
    files: dict[str, str] = {"server.js": source}
    if package_json is not None:
        files["package.json"] = package_json
    assessment, _ = _assess(files, _node_intent(health_path="/healthz"))
    assert assessment is ReleaseAssessment.candidate


def test_health_node_root_is_always_served_by_catchall() -> None:
    assessment, _ = _assess({"server.js": NODE_SERVER}, _node_intent(health_path="/"))
    assert assessment is ReleaseAssessment.candidate


_FLASK_HEALTHZ = (
    "from flask import Flask\n"
    "app = Flask(__name__)\n\n\n"
    "@app.route('/healthz')\n"
    "def hz():\n"
    "    return 'ok'\n"
)
_FASTAPI_HEALTHZ = (
    "from fastapi import FastAPI\n"
    "app = FastAPI()\n\n\n"
    "@app.get('/healthz')\n"
    "def hz():\n"
    "    return {'ok': True}\n"
)


def test_health_python_route_decorator_is_candidate_flask() -> None:
    files = {"requirements.txt": "flask\ngunicorn\n", "main.py": _FLASK_HEALTHZ}
    intent = ReleaseIntent(
        runtime=RuntimeStrategy.python, start_cmd=("gunicorn", "main:app"), health_path="/healthz"
    )
    result = detect_release(files, intent=intent, provenance=Provenance())
    assert result.assessment is ReleaseAssessment.candidate


def test_health_python_route_decorator_is_candidate_fastapi() -> None:
    files = {"requirements.txt": "fastapi\nuvicorn\n", "main.py": _FASTAPI_HEALTHZ}
    intent = ReleaseIntent(
        runtime=RuntimeStrategy.python, start_cmd=("uvicorn", "main:app"), health_path="/healthz"
    )
    result = detect_release(files, intent=intent, provenance=Provenance())
    assert result.assessment is ReleaseAssessment.candidate


def test_health_python_stray_literal_in_docs_fails_closed() -> None:
    files = {
        "requirements.txt": "fastapi\nuvicorn\n",
        "main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
        "docs/example.py": "EXAMPLE = '/healthz'\n",
    }
    intent = ReleaseIntent(
        runtime=RuntimeStrategy.python, start_cmd=("uvicorn", "main:app"), health_path="/healthz"
    )
    result = detect_release(files, intent=intent, provenance=Provenance())
    assert result.assessment is ReleaseAssessment.needs_review
    assert any(blocker.code == "health_path_unresolved" for blocker in result.blockers)


# ---------------------------------------------------------------------------------------
# DEFECT 3 — SYNTAX-01/02: invalid bounded JS fails closed; the lexical mask is preserved.
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "const = ;\n" + NODE_SERVER,  # SYNTAX-01: no binding identifier, prefix
        NODE_SERVER + "\nconst broken = ;\n",  # SYNTAX-02: empty RHS, suffix
    ],
)
def test_syntax_invalid_bounded_js_fails_closed(source: str) -> None:
    assessment, codes = _assess({"server.js": source}, _node_intent())
    assert assessment is ReleaseAssessment.needs_review
    assert "entrypoint_unresolved" in codes


def test_syntax_valid_server_stays_candidate() -> None:
    assessment, _ = _assess({"server.js": NODE_SERVER}, _node_intent())
    assert assessment is ReleaseAssessment.candidate


@pytest.mark.parametrize(
    "source",
    [
        "const proof = `.listen(process.env.PORT)`;\n",  # LEX-01 template
        "const proof = /[.]listen[(]process.env.PORT[)]/;\n",  # LEX-02 regex
        "// .listen(process.env.PORT)\n/* .listen(process.env.PORT) */\n",  # LEX-03 comment
    ],
)
def test_syntax_check_preserves_lexical_mask_controls(source: str) -> None:
    # The masked listener stays UNPROVED via the port proof — never re-classified as a
    # syntax error (which would have masked the real control the LEX cases guard).
    assessment, codes = _assess({"server.js": source}, _node_intent())
    assert assessment is ReleaseAssessment.needs_review
    assert "port_contract_unresolved" in codes


# ---- white-box: the targeted invalid-token class over a valid/invalid JS corpus ----------

_VALID_JS_CORPUS = [
    "const x = 1;",
    "let y;",
    "var z = f();",
    "const {a, b} = obj;",
    "const [x] = arr;",
    "const f = (a = 1) => a;",
    "for (let i = 0; i < n; i++) {}",
    "for (;;) {}",
    "const o = {};",
    "const arr = [];",
    "obj.const = 5;",
    "x.let = 1;",
    "const banner = `Welcome ${name}`;",
    "const re = /a[.]b/;",
    "const msg = 'SAFE';",
    'const s = "un safe string!";',
    "const t = cond ? x : y;",
    "a === b;",
    "a !== b;",
    "a >= b;",
    "a <= b;",
    "x >>= 2;",
    "x += 1;",
    "x ||= y;",
    "x ??= y;",
    "module.exports = {};",
    "export default {};",
    "a = b = c;",
    "const {a = 5} = obj;",
    "const [p = 1] = arr;",
    "function f({a = 1} = {}) { return a; }",
    "arr.map(x => x);",
    "promise.then(() => {}).catch(() => {});",
    "res.end(req.url === '/healthz' ? 'ok' : 'x');",
    "while (i <= n) { i++; }",
    "const empty = () => {};",
    "const obj = { const: 1, let: 2 };",
    "x = (y);",
]

_INVALID_JS_CORPUS = [
    "const = ;",
    "const broken = ;",
    "let x = ;",
    "var y = ;",
    "const;",
    "const = x;",
    "f(a=);",
]


@pytest.mark.parametrize("source", _VALID_JS_CORPUS)
def test_invalid_bounded_js_never_over_rejects_valid_js(source: str) -> None:
    view = js_executable_view(source)
    assert view is not None, f"valid JS masked to None: {source!r}"
    assert not _invalid_bounded_js(view, source), f"over-rejected valid JS: {source!r}"


@pytest.mark.parametrize("source", _INVALID_JS_CORPUS)
def test_invalid_bounded_js_rejects_the_targeted_token_class(source: str) -> None:
    view = js_executable_view(source)
    # Some invalid forms are already rejected by the balance/decoy pass (view is None); the
    # rest must be caught by the targeted invalid-token class.
    assert view is None or _invalid_bounded_js(view, source), f"missed invalid JS: {source!r}"
