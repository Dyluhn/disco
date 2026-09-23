<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg">
    <img src="docs/assets/logo.svg" alt="Disco." width="260">
  </picture>
</p>

# Disco

A self-hosted **Research + Agent + Build** platform: a grounded answer engine, a
deep-research report writer, and a sandboxed build agent on one append-only event
log. Built to run with local or open-weight models, not only frontier APIs.

Design authorities, when code and prose disagree:
[`basis-of-design.md`](./docs/contracts/basis-of-design.md) and
[`event-state-contract.md`](./docs/contracts/event-state-contract.md).

## Quickstart

Linux or WSL2 host (amd64 or arm64), rootless Podman, a compose provider. No
Python, Node or `uv` on the host; the stack pulls the published images (about
5 GB the first time).

```bash
sudo apt-get update && sudo apt-get install -y git podman docker-compose   # Debian 13
# Ubuntu 24.04: install git podman docker-compose-v2 instead.
# Ubuntu 24.04: ... git podman podman-compose
git clone https://github.com/Dyluhn/disco.git
cd disco
systemctl --user daemon-reload
systemctl --user start dbus.socket
loginctl enable-linger "$USER"
systemctl --user enable --now podman.socket
export DISCO_SANDBOX_SOCKET=$XDG_RUNTIME_DIR/podman/podman.sock
./deploy/compose/disco-compose up -d
podman ps --format '{{.Names}} {{.Status}}'      # three containers Up (healthy)
```

Open **http://localhost:8088**. A browser on the same machine pairs itself; if it
asks for a token, print it with
`podman compose exec app-server python -m disco.app_server.pairing_cli`.
Then add a driver model in **Settings → Models & Providers** and prove it:

```bash
podman compose exec agent-server disco-verify --quick
```

[`docs/self-host.md`](./docs/self-host.md) has the rest: the rootless Docker
path, what each compose provider prints, the headless (no browser) provider
setup, updating, starting at boot, backups, models and hardware. Something not
working? [`docs/troubleshooting.md`](./docs/troubleshooting.md) starts with one
command that says what is wrong.

## What it looks like

| Search and Deep Research | A site built with Disco |
|---|---|
| ![The home screen: one prompt box, Search or Deep Research](docs/assets/screenshots/home.png) | ![Northlake Roasters, a North Shore coffee-company site built with Disco](docs/assets/showcase/northlake/desktop.png) |

**A Deep Research report** — how the Black Death reshaped Europe's labour market: 54
searches, 16 sources, every sentence checked against the evidence (37 of 137 supported,
the rest marked, never hidden). [Dark theme](docs/assets/screenshots/research-report-dark.png).

![A Deep Research report: the evidence meter, the section outline, cited sentences](docs/assets/screenshots/research-report.png)

**From that report, without leaving the page:** a slide deck, a print-ready PDF, and a
spoken overview.

![The nine-slide deck Disco built from the report](docs/assets/showcase/deck.gif)

| [PDF export](docs/assets/showcase/black-death-labour-markets.pdf) | [Single-voice audio overview](docs/assets/showcase/audio-overview-clip.mp3) (5:35; first minute) |
|---|---|
| ![The PDF's cover page](docs/assets/showcase/report-pdf-cover.png) | ![The audio overview player at the end of the report](docs/assets/showcase/audio-overview-player.png) |

**A coffee-company site built with Disco: Northlake Roasters**, on the North Shore of Lake Superior.

| Desktop | Phone |
|---|---|
| ![Northlake Roasters: small-batch coffee roasted by the lake](docs/assets/showcase/northlake/desktop.png) | ![Northlake Roasters on a phone](docs/assets/showcase/northlake/mobile.png) |

[See the full coffee-company page](docs/assets/showcase/northlake/full.png).

**Compose export example:** [Ledgerline's dashboard](docs/assets/showcase/ledgerline/dashboard.png),
[`docker-compose.yml`](docs/assets/showcase/ledgerline/docker-compose.yml), and
[the generated README](docs/assets/showcase/ledgerline/README.md) document a separate
FastAPI + SQLite and React app brought up with `docker compose up -d --build`.

The coffee-site captures show the exported launch-site demo. The research and
Compose captures are from the original VM validation; the research deck is also
available as [PowerPoint](docs/assets/showcase/black-death-labour-markets.pptx).

## Benchmarks

[Benchmark source and evidence repository](https://github.com/Dyluhn/disco-releases) ·
[Download the benchmark release](https://github.com/Dyluhn/disco-releases/releases/tag/benchmarks-2026-09-21) ·
[Methodology and score-audit instructions](https://agenticdisco.pages.dev/docs/benchmarks)

### DeepResearch Bench — ten-task local comparison

| System | Report quality (RACE) | Supported citations per report | Citation accuracy (FACT) |
|---|---:|---:|---:|
| Claude Research (Opus 4.7) | 52.4 | 17 | 92.3% |
| Gemini Deep Research (Gemini 3.8 Flash) | 51.7 | 57 | 52.8% |
| **Disco (Qwen3.8 Flash Next)** | **50.9** | **117** | **70.5%** |
| Perplexity Deep Research | 46.9 | 56 | 65.1% |

One run per system per task, scored by an equal-weight panel of four judges.
RACE measures report quality relative to a human-written reference (50 = parity).
FACT measures citation support in retrieved source text, not whether a claim is
true. These ten tasks are a local reproduction of
[DeepResearch Bench](https://github.com/Ayanami0730/deep_research_bench), not the
100-task leaderboard; the scores are not comparable to its published rankings.

### App-Bench — six-task local comparison

| System | Verified rubric items |
|---|---:|
| Codex CLI 0.154.0 | 89.7% |
| **Disco** | **77.9%** |
| OpenHands 1.16.0 | 38.2% |
| Dyad 1.16.0 | 0% |

All four systems used DeepSeek V4 Flash (July 31 release), the same frozen
prompts and offline reference packs, a registry/model-only network, and a
two-hour cap. Each app had one build attempt and one blind grader. Scores cover
136 of 151 rubric items: an item unverified for any system was excluded for all
systems on that task. This is a local comparison, not an official App-Bench
score. Deployment and grading exceptions are documented in the fairness report.

The [benchmark release](https://github.com/Dyluhn/disco-releases/releases/tag/benchmarks-2026-09-21)
includes frozen tasks, harness and grading code, item-level results, all 24
App-Bench generated application source archives, and the full published
DeepResearch evidence. The compact source bundle includes `verify_scores.py`
to recompute the App-Bench totals. Read the included methodology, fairness
disclosures, redaction ledgers, and checksum manifests alongside the results.

## Surfaces

| Surface | What it does |
|---|---|
| **Search** | One grounded answer — claims tethered to extracted passages, with a citation-verification pass that marks unsupported claims instead of hiding them. |
| **Deep Research** | A multi-source report: investigation plan → adaptive search/read/gap loop → evidence-led outline → cited synthesis and claim verification; depth tiers, replay, export (Markdown / PDF / DOCX), audio overview, slide deck. |
| **Build** | A coding agent in a sandbox: shell sessions, dev-server preview, browser automation, a persistent IPython kernel, artifacts, plan + risk-gated execution, workspace versions and rollback. |
| **Agent** | The same machinery as Build, framed as a general task agent; extend it with MCP servers. |

What is live in each, and the known limits: [`docs/overview.md`](./docs/overview.md).

## Layout

```
.
├── packages/        uv workspace: core → tools/retrieval → agent-server → app-server
├── frontend/        Vite/React/TS web UI
├── deploy/          compose and sandbox images
├── integrations/    messaging integrations
├── prompts/         prompt assets
├── docs/            user docs: self-host, overview, contracts/, architecture.generated.md
├── development/     architecture authorities, gate scripts, governance, harness, tests, notes
├── compose.yaml · pyproject.toml · uv.lock · Makefile
```

Dependency direction is one way, `core → tools → agent-server → app-server`, and
`disco` is a PEP 420 namespace package shared by every member.

## Develop

Requires [uv](https://docs.astral.sh/uv/), Python 3.12+, Node 22. To run the
stack from your checkout instead of the published images:

```bash
podman compose -f compose.yaml -f compose.build.yaml up -d --build
```

```bash
uv sync --all-packages
uv run pytest -m "not integration"
uv run ruff check packages development/harness
uv run basedpyright
cd frontend && npm ci && npm run typecheck:build && npm run build && npm test
```

`make test` is the fast hermetic gate. The contributor guide, the monorepo
layering rule, and the maintainer landing procedure are in
[`CONTRIBUTING.md`](./CONTRIBUTING.md); the governance model (sealed
authorities, the twelve gates) is under [`development/governance/`](./development/governance/README.md).
Deep Research runs can be injected, replayed and inspected with the
[outside-observer harness](./development/harness/DEEP_RESEARCH.md).

GitHub Actions is the record on `main`. Two of the gates (public API, test
inventory) pass only on a landing head or its source sibling by design, so
branch and pull-request runs show those two red; everything else in the
required job must be green there too.

## License

Apache License 2.0 — see [`LICENSE`](./LICENSE). Use it, modify it, run it
commercially, fold it into your own product; keep the notices.

**This license will never change.** Disco will not be relicensed to a
source-available, "fair-source", BSL/SSPL or commercial license — not as it grows,
not after adoption, not on acquisition. The point of the project is to be the
trustworthy one you can self-host; a license rug-pull would break that promise.
