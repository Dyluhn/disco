import { expect, test, type APIRequestContext } from "@playwright/test";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import { decideVerification } from "../src/lib/harness/verificationCapture";

/**
 * P1B-LIVE-STABILITY — ONE stability build (single build, NO retry-to-pass).
 *
 * Drives one real autonomous MiniMax build, captures all SEVEN slices incl. the SIDECAR slice via
 * a LEDGER-NATIVE per-run relay-offset window, runs the orphan-process check, classifies against
 * STATIC_SMOKE_STRICT (which ENFORCES sidecar), and writes a per-run record JSON. The test passes
 * iff classify_dossier PASS — there is NO retry; the OUTER orchestrator runs this N times and a
 * failure is recorded, never hidden. Sidecar boundary is clock-free + run-scoped: relay.jsonl is
 * append-only and runs are strictly sequential, so provider_calls_after_terminal = (relay line
 * count at settle) − (relay line count captured the moment /state first showed a terminal).
 *
 * Env: STAB_RUN_INDEX, STAB_REPORT_DIR (per-run record out-dir), DISCO_RELIABILITY_AGENT_URL,
 * DISCO_PROVIDER_LEDGER (governed) / MINIMAX_RELAY_LOG / PMX_RELAY_LOG, PMX_VENV_PY,
 * PMX_REPO_ROOT, DISCO_RELIABILITY_PAIRING_TOKEN.
 */

// The agent-server base is ENV-FIRST: a governed run stands up an ISOLATED stack on its own
// port, so a hardcoded :8000 silently drives whatever happens to occupy that port on the host
// — or nothing at all. The literal stays only as the split-origin dev default.
const API = process.env.DISCO_RELIABILITY_AGENT_URL ?? "http://127.0.0.1:8000";
// This file lives at `current/frontend/e2e-live/`, so the repo root is THREE
// levels up; `../..` landed on `current/`, which made every PYTHONPATH entry
// below point at a `current/current/...` that does not exist and the classifier
// die with `ModuleNotFoundError`.
const REPO_ROOT =
  process.env.PMX_REPO_ROOT ??
  path.resolve(path.dirname(new URL(import.meta.url).pathname), "../../..");
// relay WRITES MINIMAX_RELAY_LOG (minimax_relay.py); primary, with PMX_RELAY_LOG legacy alias.
// DISCO_PROVIDER_LEDGER is the GOVERNED runner's ledger var — without it a governed run reads
// no ledger, skips as "relay down", and the provider-identity assertions never adjudicate.
const RELAY_LOG =
  process.env.MINIMAX_RELAY_LOG ?? process.env.PMX_RELAY_LOG ?? process.env.DISCO_PROVIDER_LEDGER ?? "";
const VENV_PY = process.env.PMX_VENV_PY ?? "python3";
const RUN_INDEX = process.env.STAB_RUN_INDEX ?? "0";
const REPORT_DIR = process.env.STAB_REPORT_DIR ?? path.join(os.tmpdir(), "disclaude-stability");
const PROMPT = "Build a simple one-page static website for a neighbourhood coffee shop.";

type EventJson = {
  seq?: number;
  kind?: string;
  status?: string;
  action_id?: string;
  id?: string;
  tool_call?: { tool_name?: string; arguments?: Record<string, unknown> };
  tool_result?: { success?: boolean; structured?: { passed?: boolean } | null };
};

function relayLineCount(): number {
  try {
    return fs.readFileSync(RELAY_LOG, "utf-8").split("\n").filter(Boolean).length;
  } catch {
    return 0;
  }
}

function relayWindow(nStart: number, nEnd: number): Array<{ host: string; model: string }> {
  const lines = fs.readFileSync(RELAY_LOG, "utf-8").split("\n").filter(Boolean);
  return lines.slice(nStart, nEnd).map((l) => JSON.parse(l)).map((r) => ({ host: r.host, model: r.model }));
}

/**
 * Pair this API context when the agent-server challenges it.
 *
 * A governed ISOLATED stack is fresh, so its first `POST /conversations` returns 401 and the
 * JSON parse blows up with a misleading SyntaxError. This is the product's own documented dev
 * pairing flow — the same one `reliability-helpers.ts` uses — and it is a NO-OP when the
 * context is already authorized. It authenticates the harness; it does not weaken auth.
 */
type SessionJson = { authenticated?: boolean; csrf_token?: string };

/** The paired session's CSRF token — every mutating request must carry it plus an Origin. */
let CSRF = "";

async function readSession(request: APIRequestContext): Promise<SessionJson> {
  const probe = await request.get(`${API}/api/auth/session`, { timeout: 10_000 });
  return probe.ok() ? ((await probe.json()) as SessionJson) : {};
}

async function ensurePaired(request: APIRequestContext): Promise<void> {
  // `/api/auth/session` is a PUBLIC route: it answers 200 `{authenticated:false}` for an
  // unauthenticated caller. Gating on `.ok()` would therefore always short-circuit and never
  // pair — read the flag, not the status.
  let session = await readSession(request);
  if (session.authenticated !== true) {
    let pairingToken = process.env.DISCO_RELIABILITY_PAIRING_TOKEN?.trim() ?? "";
    if (!pairingToken) {
      const pairing = await request.get(`${API}/api/auth/pairing-token`, { timeout: 10_000 });
      if (pairing.ok()) pairingToken = String((await pairing.json()).pairing_token ?? "");
    }
    const minted = await request.post(`${API}/api/auth/mint`, {
      data: pairingToken ? { pairing_token: pairingToken } : {},
      headers: { Origin: new URL(API).origin },
      timeout: 10_000,
    });
    expect(minted.ok(), `agent-server pairing failed: ${minted.status()} ${await minted.text()}`).toBe(true);
    session = await readSession(request);
  }
  expect(session.authenticated === true && Boolean(session.csrf_token), "no CSRF session").toBe(true);
  CSRF = String(session.csrf_token);
}

/** A mutating call carrying the paired session's Origin + CSRF (the product's own contract). */
async function postAuthed(request: APIRequestContext, url: string, data: unknown, timeout: number) {
  return request.fetch(url, {
    method: "POST",
    ...(data === undefined ? {} : { data }),
    headers: { Origin: new URL(API).origin, "X-Disco-CSRF": CSRF },
    timeout,
  });
}

type CleanupObservation = { orphans: number; workspace_released: boolean; scope: string };

/**
 * Release the conversation and observe cleanup the way the governed build_soak runner does:
 * `workspace_released` comes from the PRODUCT'S OWN kill response (2xx + killed + IDLE), never
 * from the absence of containers, and the orphan count is scoped to THIS conversation.
 *
 * Deriving release from an empty container list is a false affordance: it reads TRUE on a host
 * where podman is simply unavailable, and on the process backend where no container is ever
 * created. `scope` names what was actually probed, so the slice is self-describing.
 */
async function killConversation(request: APIRequestContext, cid: string): Promise<CleanupObservation> {
  let released = false;
  try {
    const res = await postAuthed(request, `${API}/conversations/${cid}/kill`, undefined, 15_000);
    const body = (await res.json().catch(() => ({}))) as { killed?: boolean; state?: { execution_status?: string } };
    released = res.ok() && body.killed === true && String(body.state?.execution_status ?? "") === "IDLE";
  } catch {
    released = false;
  }
  await new Promise((r) => setTimeout(r, 5_000));
  let containerNames: string[] | null = null;
  try {
    containerNames = execFileSync("podman", ["ps", "-a", "--format", "{{.Names}}"], {
      encoding: "utf-8",
      timeout: 15_000,
    })
      .split("\n")
      .filter(Boolean);
  } catch {
    containerNames = null; // podman unavailable — the probe was NOT run
  }
  return {
    orphans: (containerNames ?? []).filter((n) => n.includes(cid)).length,
    workspace_released: released,
    scope: containerNames === null ? "container-probe-unavailable" : "conversation",
  };
}

async function getJson(request: APIRequestContext, url: string) {
  const res = await request.get(url, { timeout: 15_000 });
  if (!res.ok()) throw new Error(`GET ${url} -> ${res.status()}`);
  return res.json();
}

async function allEvents(request: APIRequestContext, cid: string): Promise<EventJson[]> {
  const out: EventJson[] = [];
  let after = 0;
  for (let p = 0; p < 100; p++) {
    const body = await getJson(request, `${API}/conversations/${cid}/events?limit=200&after_seq=${after}`);
    const batch: EventJson[] = body.events ?? [];
    if (batch.length === 0) break;
    out.push(...batch);
    after = batch[batch.length - 1].seq ?? after;
  }
  return out;
}

test("one stability build → classify_dossier PASS (static_smoke_strict)", async ({ page, request }) => {
  test.setTimeout(600_000);
  fs.mkdirSync(REPORT_DIR, { recursive: true });
  const record: Record<string, unknown> = { run_index: RUN_INDEX, ts: new Date().toISOString() };
  const recordPath = path.join(REPORT_DIR, `run-${RUN_INDEX}.json`);
  const finish = (extra: Record<string, unknown>) =>
    fs.writeFileSync(recordPath, JSON.stringify({ ...record, ...extra }, null, 2), "utf-8");

  let healthy = false;
  try {
    healthy = (await request.get(`${API}/health`, { timeout: 5_000 })).ok();
  } catch {
    healthy = false;
  }
  if (!healthy || !RELAY_LOG || !fs.existsSync(RELAY_LOG)) {
    finish({ verdict: { status: "SKIPPED" }, reason: "stack/relay down" });
    test.skip(true, "stack or relay not available");
    return;
  }

  await ensurePaired(request);

  // Count the conversation socket by its PATH (`/ws/conversations/`), not by a port: the UI
  // reaches the agent-server either split-origin (ws://host:8000/ws/conversations/…) or through
  // the same-origin proxy prefix (ws://host:<ui-port>/svc/agent/ws/conversations/…). A port
  // match silently counts ZERO in the proxied topology, failing the slice closed for a TOPOLOGY
  // reason rather than a product one. The path excludes vite HMR and `/ws/research` alike.
  const wsUrls = new Set<string>();
  page.on("websocket", (ws) => {
    if (ws.url().includes("/ws/conversations/")) wsUrls.add(ws.url());
  });

  // ── drive ONE build; capture the relay offset at the MOMENT a terminal is first observed.
  const nStart = relayLineCount();
  const created = await (
    await postAuthed(request, `${API}/conversations`, { surface: "build", autonomous: true }, 15_000)
  ).json();
  const cid: string = created.conversation_id;
  record.cid = cid;
  await postAuthed(request, `${API}/conversations/${cid}/messages`, { content: PROMPT }, 20_000);

  let terminal = "";
  let nTerminal = -1;
  const statuses: string[] = [];
  for (let i = 0; i < 300; i++) {
    // Snapshot the relay offset BEFORE the state read, so any provider call that races the
    // terminal (lands between this snapshot and our observing the terminal) is counted as
    // post-terminal — never swallowed into the baseline (which would UNDER-count a leak).
    const nBefore = relayLineCount();
    const s = String((await getJson(request, `${API}/conversations/${cid}/state`)).execution_status ?? "");
    if (statuses[statuses.length - 1] !== s) statuses.push(s);
    if (["FINISHED", "VERIFIED", "STUCK", "ERROR", "AWAITING_USER"].includes(s)) {
      terminal = s;
      nTerminal = nBefore; // conservative boundary (fail-closed: cannot hide a post-terminal call)
      break;
    }
    await new Promise((r) => setTimeout(r, 3_000));
  }
  if (!/FINISHED|VERIFIED/.test(terminal)) {
    finish({ verdict: { status: "FAIL", code: "NO_CLEAN_TERMINAL" }, terminal, statuses });
    expect(terminal, `run ${RUN_INDEX}: build terminal=${terminal} (statuses ${statuses.join(",")})`).toMatch(
      /FINISHED|VERIFIED/,
    );
    return;
  }

  // ── SETTLE: confirm the run stayed terminal while no new provider calls landed.
  for (let i = 0; i < 4; i++) {
    await new Promise((r) => setTimeout(r, 2_500));
    await getJson(request, `${API}/conversations/${cid}/state`);
  }
  const nSettle = relayLineCount();
  const sidecar = {
    stopped_at_terminal: nSettle === nTerminal,
    provider_calls_after_terminal: nSettle - nTerminal,
  };

  // ── event-log slices.
  const events = await allEvents(request, cid);
  const previewStart = events.find((e) => e.tool_call?.tool_name === "preview_start");
  const manualPort = Boolean((previewStart?.tool_call?.arguments ?? {}).port);
  const lifecycleStatuses = events.filter((e) => e.kind === "status" && e.status).map((e) => e.status!);
  // disco verifies via verify_web_app OR a browser inspection of the served app; decideVerification
  // mirrors disco's actual finish-verification gate (fail-closed on an unverified release).
  const verification = decideVerification(events, terminal);

  // ── UI slices.
  await page.setViewportSize({ width: 1400, height: 1600 });
  await page.goto(`/build/${cid}`, { timeout: 30_000 });
  const feedRow = page.locator("button", { has: page.locator("span.truncate") });
  const artifactShown = await feedRow.first().isVisible({ timeout: 30_000 }).catch(() => false);
  await page.getByRole("tab", { name: /preview/i }).first().click({ timeout: 15_000 }).catch(() => {});
  let previewShown = false;
  const deadline = Date.now() + 90_000;
  while (Date.now() < deadline && !previewShown) {
    const txt = await page.frameLocator("iframe").first().locator("body").innerText({ timeout: 5_000 }).catch(() => "");
    previewShown = txt.trim().length > 50;
    if (!previewShown) {
      await page.getByRole("button", { name: /refresh/i }).first().click({ timeout: 2_000 }).catch(() => {});
      await page.waitForTimeout(4_000);
    }
  }
  const screenshot = path.join(REPORT_DIR, `run-${RUN_INDEX}.png`);
  await page.screenshot({ path: screenshot, fullPage: true });
  const browserWsConnections = wsUrls.size;

  // ── cleanup: release observed from the product's own kill response, orphans scoped to this cid.
  const cleanup = await killConversation(request, cid);

  // ── per-run provider ledger window (THIS run only) — minimax-only / 0 openrouter.
  const providerRecords = relayWindow(nStart, nSettle);
  const ledgerSummary = {
    count: providerRecords.length,
    hosts: [...new Set(providerRecords.map((r) => r.host))],
    models: [...new Set(providerRecords.map((r) => r.model))],
    openrouter: providerRecords.filter((r) => String(r.host).toLowerCase().includes("openrouter")).length,
  };

  const productEvidence = {
    browser_ws: { connections: browserWsConnections },
    lifecycle: { terminal, statuses: lifecycleStatuses },
    sidecar,
    preview: { owner: "platform", manual_port: manualPort },
    shown: { artifact_shown: artifactShown, preview_shown: previewShown },
    verification,
    cleanup,
  };

  const work = fs.mkdtempSync(path.join(os.tmpdir(), "disclaude-stab-"));
  const capPath = path.join(work, "captured.json");
  fs.writeFileSync(
    capPath,
    JSON.stringify({
      product_evidence: productEvidence,
      provider_records: providerRecords,
      events,
      autonomous: true,
      run_id: cid,
      scenario_id: "static_smoke_strict",
    }),
    "utf-8",
  );

  // classify_captured prints the verdict JSON to stdout, then exits NON-ZERO on any non-PASS
  // verdict (by design). execFileSync throws on non-zero exit — so we must parse the REAL verdict
  // from stdout in BOTH branches; CLASSIFIER_ERROR is reserved for genuinely-unparseable output.
  const parseVerdict = (out: string): { status?: string; code?: string } | null => {
    const last = out.trim().split("\n").pop() ?? "";
    try {
      return JSON.parse(last);
    } catch {
      return null;
    }
  };
  let verdict: { status?: string; code?: string } = {};
  const classifyArgs = ["-m", "harness.product_build.classify_captured", capPath, path.join(work, "dossier")];
  const classifyOpts = {
    cwd: REPO_ROOT,
    encoding: "utf-8" as const,
    timeout: 60_000,
    env: {
      ...process.env,
      // `development` carries the `harness` package being run as `-m`; it is not
      // an installed distribution, so nothing else puts it on the path.
      PYTHONPATH: ["current/packages/core/src", "current/packages/tools/src", "current/packages/agent-server/src", "development", "."]
        .map((p) => path.join(REPO_ROOT, p))
        .join(":"),
    },
  };
  try {
    verdict = parseVerdict(execFileSync(VENV_PY, classifyArgs, classifyOpts)) ?? { status: "CLASSIFIER_ERROR" };
  } catch (e) {
    const err = e as { stdout?: string; stderr?: string };
    verdict = parseVerdict(err.stdout ?? "") ?? {
      status: "CLASSIFIER_ERROR",
      code: `${err.stdout ?? ""}${err.stderr ?? String(e)}`.slice(0, 400),
    };
  }

  finish({
    verdict,
    terminal,
    sidecar,
    ledger_summary: ledgerSummary,
    product_evidence: productEvidence,
    screenshot,
    dossier: path.join(work, "dossier"),
  });

  expect(ledgerSummary.openrouter, `run ${RUN_INDEX}: OpenRouter calls in ledger`).toBe(0);
  expect(
    ledgerSummary.hosts.every((h) => String(h).toLowerCase().includes("minimax")),
    `run ${RUN_INDEX}: non-minimax host in ledger: ${JSON.stringify(ledgerSummary.hosts)}`,
  ).toBeTruthy();
  expect(verdict.status, `run ${RUN_INDEX}: classify_dossier=${JSON.stringify(verdict)}`).toBe("PASS");
});
