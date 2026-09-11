# SOTA Report — self-hosted-ai-workspace

- **Date:** 2026-09-10
- **Domain:** self-hosted-ai-workspace
- **Tier:** BEHIND
- **Mode:** standard

## Summary

**BEHIND · 12/16 table-stakes met · 0/4 edge**

The engineering is ahead of its cluster; the distribution is missing entirely. All four table-stakes gaps are release engineering, not capability.

## Capability matrix

| Capability                  | Us  | SOTA                                | Gap?         | Reference                                                                                                                |
| --------------------------- | --- | ----------------------------------- | ------------ | ------------------------------------------------------------------------------------------------------------------------ |
| one-command-selfhost        | ✅   | all 9 direct peers                  |              | [open-webui/open-webui](https://github.com/open-webui/open-webui) → docker-compose.yaml                                  |
| prebuilt-images             | ❌   | all 9 direct peers                  | table-stakes | [open-webui/open-webui](https://github.com/open-webui/open-webui) → ghcr.io/open-webui/open-webui in docker-compose.yaml |
| versioned-releases          | ❌   | all 9 (30+ published releases each) | table-stakes | [danny-avila/LibreChat](https://github.com/danny-avila/LibreChat) → tagged releases (v0.8.8-rc2)                         |
| public-availability         | ❌   | all 9 direct peers                  | table-stakes | [onyx-dot-app/onyx](https://github.com/onyx-dot-app/onyx) → public repo                                                  |
| docs-site                   | ❌   | all 9 have a homepage               | table-stakes | [lfnovo/open-notebook](https://github.com/lfnovo/open-notebook) → open-notebook.ai                                       |
| multi-provider-llm          | ✅   | all 9 direct peers                  |              | [danny-avila/LibreChat](https://github.com/danny-avila/LibreChat) → multi-provider switching                             |
| local-model-support         | ✅   | all 9 direct peers                  |              | [open-webui/open-webui](https://github.com/open-webui/open-webui) → Ollama support                                       |
| web-search-grounding        | ✅   | all 9 direct peers                  |              | [ItzCrazyKns/Perplexica](https://github.com/ItzCrazyKns/Perplexica) → search grounding                                   |
| document-rag                | ✅   | all 9 direct peers                  |              | [Mintplex-Labs/anything-llm](https://github.com/Mintplex-Labs/anything-llm) → workspaces                                 |
| mcp-client                  | ✅   | all 9 direct peers                  |              | [danny-avila/LibreChat](https://github.com/danny-avila/LibreChat) → MCP support                                          |
| conversation-persistence    | ✅   | all 9 direct peers                  |              | [onyx-dot-app/onyx](https://github.com/onyx-dot-app/onyx) → chat history                                                 |
| web-ui                      | ✅   | all 9 direct peers                  |              | [open-webui/open-webui](https://github.com/open-webui/open-webui) → web UI                                               |
| auth                        | ✅   | all 9 direct peers                  |              | [danny-avila/LibreChat](https://github.com/danny-avila/LibreChat) → auth                                                 |
| sandboxed-code-execution    | ✅   | OpenHands, Suna                     |              | [All-Hands-AI/OpenHands](https://github.com/All-Hands-AI/OpenHands) → Docker sandbox runtime                             |
| citation-grounded-answers   | ✅   | SurfSense, local-deep-research      |              | [LearningCircuit/local-deep-research](https://github.com/LearningCircuit/local-deep-research) → citation verification    |
| export-artifacts            | ✅   | Open Notebook, Onyx                 |              | [lfnovo/open-notebook](https://github.com/lfnovo/open-notebook) → podcast + export                                       |
| multi-user-rbac             | ❌   | LibreChat, Onyx, open-webui         | edge         | [danny-avila/LibreChat](https://github.com/danny-avila/LibreChat) → LDAP/SSO                                             |
| observability-otel          | ❌   | Onyx only (OpenHands: 0 hits)       | edge         | [onyx-dot-app/onyx](https://github.com/onyx-dot-app/onyx) → OpenTelemetry instrumentation                                |
| helm-k8s                    | ❌   | Onyx, open-webui                    | edge         | [onyx-dot-app/onyx](https://github.com/onyx-dot-app/onyx) → helm/onyx-0.8.22 release                                     |
| published-quality-benchmark | ⚠️  | local-deep-research, OpenHands      | edge         | [LearningCircuit/local-deep-research](https://github.com/LearningCircuit/local-deep-research) → ~95% SimpleQA            |

## Actionable gaps (high confidence)

- **Versioned releases** (table-stakes) — Every direct peer ships 30+ published releases. This repo has exactly one tag (v0.1.0, 2026-07-03, pushed to origin) with a matching CHANGELOG section, but zero published GitHub releases and 1236 commits of drift on HEAD since it. A self-hoster still cannot pin a known-good version.
  - Study: [danny-avila/LibreChat](https://github.com/danny-avila/LibreChat) → tagged releases + per-version changelog
  - Step 1: Cut the next tag for the 1236 commits since v0.1.0: add a '## v0.2.0' section to CHANGELOG.md (promoting the current '## Unreleased' body) and push tag v0.2.0. .github/workflows/release.yml triggers on v*, runs every fitness gate and greps CHANGELOG.md for '^## v?<version>'. Note it only DRAFTS the release — v0.1.0 produced no published release, so the draft must also be published.
- **Prebuilt container images** (table-stakes) — The quickstart compiles four images on the user's machine before first light; every peer pulls a published image instead.
  - Study: [open-webui/open-webui](https://github.com/open-webui/open-webui) → ghcr.io/open-webui/open-webui in docker-compose.yaml
  - Step 1: Add a publish-images job to .github/workflows/release.yml using docker/build-push-action against ghcr.io for deploy/compose/Dockerfile.server, frontend/Dockerfile and deploy/sandbox/Dockerfile; then point compose.yaml:39/276 at the published tags.
- **Public availability** (table-stakes) — The repo carries Apache-2.0, CONTRIBUTING.md, CODE_OF_CONDUCT.md, SECURITY.md and a public-voice README with a clone URL, but is private with 0 stars. Nothing else on this list matters until a stranger can clone it.
  - Study: [onyx-dot-app/onyx](https://github.com/onyx-dot-app/onyx) → public repo + airgap compose variants
  - Step 1: Run a history-wide secret scan (gitleaks or trufflehog over full history, not just HEAD) before flipping visibility; then set repo topics and a homepage.
- **Hosted docs site** (table-stakes) — All nine direct peers have one. docs/self-host.md is strong but unreachable without cloning, which is currently impossible.
  - Study: [lfnovo/open-notebook](https://github.com/lfnovo/open-notebook) → open-notebook.ai
  - Step 1: Add .github/workflows/docs.yml publishing docs/ to GitHub Pages (mkdocs-material or Astro Starlight) with docs/self-host.md and docs/overview.md as landing pages; set the repo homepage to it.

## Audit trail

**Sources**
- [langgenius/dify](https://github.com/langgenius/dify) (popular) — 155.3k⭐
- [open-webui/open-webui](https://github.com/open-webui/open-webui) (popular) — 151.5k⭐
- [langflow-ai/langflow](https://github.com/langflow-ai/langflow) (popular) — 154.5k⭐
- [infiniflow/ragflow](https://github.com/infiniflow/ragflow) (technically advanced) — 90.4k⭐
- [All-Hands-AI/OpenHands](https://github.com/All-Hands-AI/OpenHands) (canonical) — 87.2k⭐
- [bytedance/deer-flow](https://github.com/bytedance/deer-flow) (popular) — 82.2k⭐
- [Mintplex-Labs/anything-llm](https://github.com/Mintplex-Labs/anything-llm) (popular) — 65.9k⭐
- [danny-avila/LibreChat](https://github.com/danny-avila/LibreChat) (canonical) — 43.0k⭐
- [lfnovo/open-notebook](https://github.com/lfnovo/open-notebook) (niche-relevant) — 38.5k⭐
- [khoj-ai/khoj](https://github.com/khoj-ai/khoj) (popular) — 37.2k⭐
- [ItzCrazyKns/Perplexica](https://github.com/ItzCrazyKns/Perplexica) (popular) — 36.7k⭐
- [onyx-dot-app/onyx](https://github.com/onyx-dot-app/onyx) (technically advanced) — 32.0k⭐
- [assafelovic/gpt-researcher](https://github.com/assafelovic/gpt-researcher) (canonical) — 29.4k⭐
- [letta-ai/letta](https://github.com/letta-ai/letta) (technically advanced) — 24.7k⭐
- [kortix-ai/suna](https://github.com/kortix-ai/suna) (niche-relevant) — 20.2k⭐
- [stackblitz-labs/bolt.diy](https://github.com/stackblitz-labs/bolt.diy) (stale-reference) — 19.9k⭐
- [HKUDS/DeepCode](https://github.com/HKUDS/DeepCode) (niche-relevant) — 16.5k⭐
- [MODSetter/SurfSense](https://github.com/MODSetter/SurfSense) (niche-relevant) — 16.1k⭐
- [e2b-dev/E2B](https://github.com/e2b-dev/E2B) (technically advanced) — 13.7k⭐
- [langchain-ai/open_deep_research](https://github.com/langchain-ai/open_deep_research) (stale-reference) — 12.7k⭐
- [LearningCircuit/local-deep-research](https://github.com/LearningCircuit/local-deep-research) (technically advanced) — 9.1k⭐
- [miurla/morphic](https://github.com/miurla/morphic) (niche-relevant) — 9.1k⭐

**Disclosures**

Mode `standard`: 22 repos scanned, saturation reached after 4 query rounds (the final round surfaced only sub-1k-star repos and no new table-stakes capability). Stars and push dates are exact via `gh api` on 2026-09-10, not web-scraped. The matrix is scored against the `self-hosted-ai-workspace` cluster only; capabilities belonging to other clusters are recorded as optional ideas and excluded from coverage and tier. `multi-user-rbac` is listed as edge because docs/overview.md declares Disco single-tenant by design, so it is not counted as a gap. Not verified in this environment: no `uv`, no `.venv` and no container runtime on this host, so no gate or test suite was executed as part of this scan.

CORRECTION (same day, after the initial run): the first pass measured this repository as a SHALLOW clone (254 commits, no tags fetched) and wrongly reported '0 tags, CHANGELOG has only ## Unreleased'. After `git fetch --unshallow` (2498 commits, 11 remote tags) the true state is: tag v0.1.0 exists and is pushed, CHANGELOG.md carries a '## v0.1.0 - 2026-07-03' section, and HEAD is 1236 commits ahead of it. The gap stands — 0 published GitHub releases vs 30+ for every peer — but the first step changed from 'cut v0.1.0' to 'cut the next tag and publish the draft'. A first pass also reported two required release gates (check_public_api, check_test_inventory) as failing; that was a local-clone artifact, now retracted. The clone had not fetched most of the remote's 572 refs, so authority commits appeared absent. After fetching all refs both gates pass (PUBLIC API OK; TEST INVENTORY OK, 14189 collected IDs), as do all ten gate scripts and the frontend required job (typecheck, 1535 vitest tests, production build).

UPDATE (2026-09-10, PKG-45): versioned-releases and prebuilt-images are addressed on
main — `## v0.2.0` in CHANGELOG.md, release.yml publishes multi-arch images to
ghcr.io/dyluhn/disco-{server,frontend,sandbox} after the gates, and compose.yaml
pulls them by default. public-availability follows the same package (full-history
secret scan: trufflehog 0 verified, gitleaks 184 pattern hits all fixtures/hashes).
docs-site is still open.
