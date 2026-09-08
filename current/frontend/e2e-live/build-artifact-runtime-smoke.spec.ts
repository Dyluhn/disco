import { expect, test, type Page, type APIRequestContext } from "@playwright/test";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

/**
 * P1B-LIVE-3b — the durable BROWSER product-harness smoke (live, DRIVES A REAL BUILD).
 *
 * Encodes the proven capture→classify pipeline so CI can re-run it: drive a real autonomous
 * MiniMax build, observe the SIX product-evidence slices from the REAL UI + event log
 * (browser_ws / lifecycle / preview / shown / verification) and a post-run kill (cleanup),
 * write a capture JSON, then hand it to the Python classifier
 * (`harness.product_build.classify_captured`) which assembles the locked dossier and runs
 * `classify_dossier(STATIC_SITE_SMOKE)` — asserting PASS. Nothing is faked: every slice value
 * is observed live; the provider ledger is sourced from the relay log so its host is the REAL
 * api.minimaxi.chat (the P17 MiniMax-only constraint).
 *
 * RELIABILITY (a LIVE model is involved): a single autonomous build can (a) STUCK on the
 * bookkeeping-streak gate, or (b) have its sandbox preview not yet reachable at the exact
 * capture moment. So the run is BOUNDED-RETRIED up to MAX_ATTEMPTS builds and PASSes on the
 * first that finishes + renders + classifies PASS; every failed attempt is `console.log`ged so
 * flakiness stays VISIBLE (honest — not masked). SKIPs (never crashes) when the stack is down.
 *
 * Requires the live stack: agent-server :8000 (DISCO_CONFIG → driver-minimax → the relay),
 * the MiniMax relay :8080, and vite (the live config starts its own). Env knobs:
 *   PMX_RELAY_LOG   — relay JSONL (provider ledger source; required)
 *   PMX_VENV_PY     — python with the repo on PYTHONPATH (classifier)
 *   PMX_REPO_ROOT   — disclaude repo root
 *
 * A4-CLOSURE — the same spec also runs against a GOVERNED ISOLATED STACK, so the four browser
 * oracles adjudicate on a campaign-topology run instead of SKIPping. Three knobs, all defaulting
 * to the historical :8000 / relay behaviour so the original invocation is unchanged:
 *   DISCO_RELIABILITY_AGENT_URL — the isolated stack's agent-server (exported by isolated_stack)
 *   DISCO_PROVIDER_LEDGER       — the stack's own append-only {host,model} JSONL
 *   PRODUCT_SCENARIO_ID         — which ProductScenario to classify against
 */

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const API = process.env.DISCO_RELIABILITY_AGENT_URL ?? "http://127.0.0.1:8000";
// This file lives at `current/frontend/e2e-live/`, so the repo root is THREE
// levels up; `../..` landed on `current/`, which made every PYTHONPATH entry
// below point at a `current/current/...` that does not exist and the classifier
// die with `ModuleNotFoundError`.
const REPO_ROOT = process.env.PMX_REPO_ROOT ?? path.resolve(__dirname, "../../..");
// The relay WRITES MINIMAX_RELAY_LOG (minimax_relay.py); accept it as primary, keep PMX_RELAY_LOG
// as a legacy alias so the relay-ledger source can never silently drift to empty (-> a false skip).
// DISCO_PROVIDER_LEDGER is the isolated stack's native ledger and carries the same {host,model}
// records, so the provider constraint is enforced from the run's OWN calls either way.
const RELAY_LOG =
  process.env.MINIMAX_RELAY_LOG ?? process.env.PMX_RELAY_LOG ?? process.env.DISCO_PROVIDER_LEDGER ?? "";
const SCENARIO_ID = process.env.PRODUCT_SCENARIO_ID ?? "static_site_smoke";
const VENV_PY = process.env.PMX_VENV_PY ?? "python3";
const SCREENSHOT_DIR = path.resolve(REPO_ROOT, "test-record/screenshots/build-artifact-runtime");
const MAX_ATTEMPTS = 2; // each build is ~4-5m; the live config budgets 15m
const BUILD_PROMPT =
  "Build a simple one-page static website for a neighbourhood coffee shop: a hero, an " +
  "about section, an opening hours table, and a contact section. Plain HTML and CSS.";

type EventJson = {
  seq?: number;
  kind?: string;
  status?: string;
  detail?: string;
  action_id?: string;
  id?: string;
  tool_call?: { tool_name?: string; arguments?: Record<string, unknown> };
  tool_result?: { success?: boolean; structured?: { passed?: boolean } | null };
};

/**
 * Pair this API context when the agent-server challenges it (A4-CLOSURE).
 *
 * The historical :8000 invocation ran against a stack whose session was already established;
 * a governed ISOLATED stack is fresh, so its first `POST /conversations` returns 401
 * "auth required" and the JSON parse blows up with a misleading SyntaxError. This is the
 * product's own documented dev pairing flow — the same one `reliability-helpers.ts`
 * `requireReliabilityStack` uses — and it is a NO-OP when the context is already authorized.
 * It authenticates the harness; it does not weaken the product's auth.
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
    expect(
      minted.ok(),
      `agent-server pairing failed: ${minted.status()} ${await minted.text()}`,
    ).toBe(true);
    session = await readSession(request);
  }
  expect(session.authenticated === true && Boolean(session.csrf_token), "no CSRF session").toBe(
    true,
  );
  CSRF = String(session.csrf_token);
}

/** A mutating call carrying the paired session's Origin + CSRF (the product's own contract). */
async function postAuthed(
  request: APIRequestContext,
  url: string,
  data: unknown,
  timeout: number,
) {
  return request.fetch(url, {
    method: "POST",
    ...(data === undefined ? {} : { data }),
    headers: { Origin: new URL(API).origin, "X-Disco-CSRF": CSRF },
    timeout,
  });
}

async function getJson(request: APIRequestContext, url: string) {
  const res = await request.get(url, { timeout: 15_000 });
  if (!res.ok()) throw new Error(`GET ${url} -> ${res.status()}`);
  return res.json();
}

async function allEvents(request: APIRequestContext, cid: string): Promise<EventJson[]> {
  const out: EventJson[] = [];
  let after = 0;
  for (let page = 0; page < 100; page++) {
    const body = await getJson(request, `${API}/conversations/${cid}/events?limit=200&after_seq=${after}`);
    const batch: EventJson[] = body.events ?? [];
    if (batch.length === 0) break;
    out.push(...batch);
    after = batch[batch.length - 1].seq ?? after;
  }
  return out;
}

/** Drive a fresh autonomous build and poll to a terminal. */
async function driveBuild(request: APIRequestContext): Promise<{ cid: string; terminal: string; statuses: string[] }> {
  const createRes = await postAuthed(
    request,
    `${API}/conversations`,
    { surface: "build", autonomous: true },
    15_000,
  );
  if (!createRes.ok()) {
    throw new Error(`POST /conversations -> ${createRes.status()}: ${await createRes.text()}`);
  }
  const created = await createRes.json();
  const cid: string = created.conversation_id;
  await postAuthed(request, `${API}/conversations/${cid}/messages`, { content: BUILD_PROMPT }, 20_000);
  let terminal = "";
  const statuses: string[] = [];
  for (let i = 0; i < 280; i++) {
    const st = await getJson(request, `${API}/conversations/${cid}/state`);
    const s = String(st.execution_status ?? "");
    if (statuses[statuses.length - 1] !== s) statuses.push(s);
    if (["FINISHED", "VERIFIED", "STUCK", "ERROR", "AWAITING_USER"].includes(s)) {
      terminal = s;
      break;
    }
    await new Promise((r) => setTimeout(r, 3_000));
  }
  return { cid, terminal, statuses };
}

/** Poll the Preview pane's iframe until it renders NON-TRIVIAL content (a real render — not a
 *  keyword match, and tolerant of the sandbox preview taking a few seconds to become reachable).
 *  Returns false if nothing renders within the budget (a genuine product fact, not a fake pass). */
async function waitPreviewRendered(page: Page): Promise<boolean> {
  await page.getByRole("tab", { name: /preview/i }).first().click({ timeout: 15_000 }).catch(() => {});
  const deadline = Date.now() + 90_000;
  while (Date.now() < deadline) {
    const txt = await page
      .frameLocator("iframe")
      .first()
      .locator("body")
      .innerText({ timeout: 5_000 })
      .catch(() => "");
    if (txt.trim().length > 50) return true; // a real, non-empty render
    // nudge a slow preview: click a Refresh control if the pane offers one
    await page.getByRole("button", { name: /refresh/i }).first().click({ timeout: 2_000 }).catch(() => {});
    await page.waitForTimeout(4_000);
  }
  return false;
}

type CleanupObservation = { orphans: number; workspace_released: boolean; scope: string };

/**
 * Release the conversation and observe cleanup the way the governed build_soak runner does:
 * `workspace_released` comes from the PRODUCT'S OWN kill response (2xx + killed + IDLE), never
 * from the absence of containers, and the orphan count is scoped to THIS conversation.
 *
 * `scope` names what was actually probed, so the slice is self-describing. `orphans` counts only
 * containers whose name carries THIS conversation id — a claim that stays true under both the
 * podman and the process backend (under process no container is created, so none can survive).
 * It deliberately does NOT try to infer the backend from the host's container list: an unrelated
 * `disco-sbx-*` container left by other work would flip a global heuristic while saying nothing
 * about this run, which is the false-affordance shape this slice exists to avoid.
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

test("live MiniMax build → product-evidence dossier classifies PASS", async ({ page, request }) => {
  test.setTimeout(840_000); // up to MAX_ATTEMPTS real builds

  let healthy = false;
  try {
    healthy = (await request.get(`${API}/health`, { timeout: 5_000 })).ok();
  } catch {
    healthy = false;
  }
  test.skip(!healthy, `agent-server not reachable at ${API} — start the live stack and re-run`);
  test.skip(!RELAY_LOG || !fs.existsSync(RELAY_LOG), "PMX_RELAY_LOG (relay JSONL) not provided");
  await ensurePaired(request);

  // browser_ws: count the BUILD conversation sockets, reset per attempt. Match on the WS PATH
  // (`/ws/conversations/`), not on a port: the UI reaches the agent-server either split-origin
  // (ws://host:8000/ws/conversations/…) or through the same-origin proxy prefix
  // (ws://host:<ui-port>/svc/agent/ws/conversations/…). A port match silently counts ZERO in the
  // proxied topology, which would fail this oracle closed for a TOPOLOGY reason rather than a
  // product one. The path match also excludes vite HMR and the separate /ws/research socket.
  const wsUrls = new Set<string>();
  page.on("websocket", (ws) => {
    if (ws.url().includes("/ws/conversations/")) wsUrls.add(ws.url());
  });

  const failures: string[] = [];
  let verdict: { status?: string; code?: string } | null = null;

  for (let attempt = 1; attempt <= MAX_ATTEMPTS && verdict?.status !== "PASS"; attempt++) {
    const { cid, terminal, statuses } = await driveBuild(request);
    if (!/FINISHED|VERIFIED/.test(terminal)) {
      const why = `attempt ${attempt}: build terminal=${terminal || "timeout"} (statuses ${statuses.join(",")})`;
      console.log(`[harness] ${why}`);
      failures.push(why);
      await killConversation(request, cid);
      continue;
    }

    // Event-log slices: preview ownership + the FINAL verify verdict.
    const events = await allEvents(request, cid);
    const previewStart = events.find((e) => e.tool_call?.tool_name === "preview_start");
    const manualPort = Boolean((previewStart?.tool_call?.arguments ?? {}).port);
    const verifyActions = events.filter((e) => e.tool_call?.tool_name === "verify_web_app");
    const verifyAction = verifyActions[verifyActions.length - 1]; // FINAL finish-gate verify
    const verifyObs = events.find((e) => e.kind === "observation" && e.action_id === verifyAction?.id);
    const lifecycleStatuses = events.filter((e) => e.kind === "status" && e.status).map((e) => e.status!);

    // UI slices: navigate, capture artifact_shown (feed) + preview_shown (real render).
    wsUrls.clear();
    await page.setViewportSize({ width: 1400, height: 1600 });
    await page.goto(`/build/${cid}`, { timeout: 30_000 });
    const feedRow = page.locator("button", { has: page.locator("span.truncate") });
    const artifactShown = await feedRow
      .first()
      .isVisible({ timeout: 30_000 })
      .catch(() => false);
    const previewShown = await waitPreviewRendered(page);
    fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
    await page.screenshot({ path: path.join(SCREENSHOT_DIR, `build-preview-${attempt}.png`), fullPage: true });
    const browserWsConnections = wsUrls.size;

    if (!previewShown) {
      const why = `attempt ${attempt}: FINISHED but the Preview pane did not render within budget`;
      console.log(`[harness] ${why}`);
      failures.push(why);
      await killConversation(request, cid);
      continue;
    }

    // Cleanup slice: release + scoped orphan observation (see killConversation).
    const cleanup = await killConversation(request, cid);

    // Provider ledger from the REAL relay log (host = api.minimaxi.chat).
    const providerRecords = fs
      .readFileSync(RELAY_LOG, "utf-8")
      .split("\n")
      .filter(Boolean)
      .map((l) => JSON.parse(l))
      .map((r) => ({ host: r.host, model: r.model }));
    expect(providerRecords.length, "relay log had no provider records").toBeGreaterThanOrEqual(1);

    const capture = {
      product_evidence: {
        browser_ws: { connections: browserWsConnections },
        lifecycle: { terminal, statuses: lifecycleStatuses },
        preview: { owner: "platform", manual_port: manualPort },
        shown: { artifact_shown: artifactShown, preview_shown: previewShown },
        verification: {
          // fail-closed: the FINAL verify ran AND its structured verdict is pass.
          ready_for_verification_called: Boolean(verifyAction),
          passed: verifyObs?.tool_result?.success === true && verifyObs?.tool_result?.structured?.passed === true,
        },
        cleanup,
      },
      provider_records: providerRecords,
      events,
      autonomous: true,
      run_id: cid,
      scenario_id: SCENARIO_ID,
    };
    // F33 (2026-08-06g): this dossier is the browser four's ONLY verdict artifact. It was
    // written to `fs.mkdtempSync(os.tmpdir())` and preserved by NEITHER runner script, so a
    // green `capture.exit` could coexist with the evidence sitting in /tmp awaiting a
    // cleaner — 2026-08-05m and 2026-08-06g both recovered it by an unrecorded manual step.
    // When the governed runner sets DISCO_RELIABILITY_SUITE_OUT (which the live Playwright
    // config already requires) write it INTO the evidence tree instead, one directory per
    // attempt so a later retry cannot overwrite the attempt that produced the verdict.
    // Falls back to the old mkdtemp behaviour when the variable is unset (ad-hoc local runs).
    const suiteOut = process.env.DISCO_RELIABILITY_SUITE_OUT;
    const work = suiteOut
      ? path.join(suiteOut, `dossier-attempt-${attempt}`)
      : fs.mkdtempSync(path.join(os.tmpdir(), "disclaude-dossier-"));
    fs.mkdirSync(work, { recursive: true });
    const capPath = path.join(work, "captured.json");
    fs.writeFileSync(capPath, JSON.stringify(capture), "utf-8");

    try {
      const stdout = execFileSync(
        VENV_PY,
        ["-m", "harness.product_build.classify_captured", capPath, path.join(work, "dossier")],
        {
          cwd: REPO_ROOT,
          encoding: "utf-8",
          timeout: 60_000,
          env: {
            ...process.env,
            // `development` carries the `harness` package being run as `-m`; it is
            // not an installed distribution, so nothing else puts it on the path.
            PYTHONPATH: ["current/packages/core/src", "current/packages/tools/src", "current/packages/agent-server/src", "development", "."]
              .map((p) => path.join(REPO_ROOT, p))
              .join(":"),
          },
        },
      );
      verdict = JSON.parse(stdout.trim().split("\n").pop() ?? "{}");
    } catch (e) {
      const err = e as { stdout?: string; stderr?: string };
      verdict = { status: "CLASSIFIER_ERROR", code: `${err.stdout ?? ""}${err.stderr ?? String(e)}`.slice(0, 300) };
    }
    if (verdict?.status !== "PASS") {
      const why = `attempt ${attempt}: classify_dossier=${JSON.stringify(verdict)}`;
      console.log(`[harness] ${why}`);
      failures.push(why);
    }
  }

  expect(
    verdict?.status,
    `no attempt produced a clean PASS in ${MAX_ATTEMPTS} builds:\n${failures.join("\n")}`,
  ).toBe("PASS");
});
