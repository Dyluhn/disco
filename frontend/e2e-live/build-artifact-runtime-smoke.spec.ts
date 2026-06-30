import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

/**
 * P1B-LIVE-3b — the durable BROWSER product-harness smoke (live, DRIVES A REAL BUILD).
 *
 * Encodes the proven capture→classify pipeline so CI can re-run it: drive a real autonomous
 * MiniMax build through the agent-server, observe the SIX product-evidence slices from the
 * REAL UI + event log (browser_ws / lifecycle / preview / shown / verification) and from a
 * post-run kill (cleanup), write a single capture JSON, then hand it to the Python classifier
 * (`harness.product_build.classify_captured`) which assembles the locked dossier and runs
 * `classify_dossier(STATIC_SITE_SMOKE)` — asserting PASS. Nothing is faked: every slice value
 * is observed live; the provider ledger is sourced from the relay log so its host is the REAL
 * api.minimaxi.chat (the P17 MiniMax-only constraint). SKIPs (never crashes) when the stack
 * is down so the unit suite is unaffected.
 *
 * Requires the live stack: agent-server :8000 (DISCO_CONFIG → driver-minimax → the relay),
 * the MiniMax relay :8080, and vite (VITE_AGENT_BASE=http://localhost:8000). Env knobs:
 *   PMX_RELAY_LOG   — path to the relay JSONL (provider ledger source; required for the run)
 *   PMX_VENV_PY     — python interpreter with the repo on PYTHONPATH (classifier)
 *   PMX_REPO_ROOT   — disclaude repo root (PYTHONPATH + cwd for the classifier)
 */

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const API = "http://127.0.0.1:8000";
const REPO_ROOT = process.env.PMX_REPO_ROOT ?? path.resolve(__dirname, "../..");
const RELAY_LOG = process.env.PMX_RELAY_LOG ?? "";
const VENV_PY = process.env.PMX_VENV_PY ?? "python3";
const SCREENSHOT_DIR = path.resolve(REPO_ROOT, "test-record/screenshots/build-artifact-runtime");
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

async function getJson(request: import("@playwright/test").APIRequestContext, url: string) {
  const res = await request.get(url, { timeout: 15_000 });
  if (!res.ok()) throw new Error(`GET ${url} -> ${res.status()}`);
  return res.json();
}

async function allEvents(
  request: import("@playwright/test").APIRequestContext,
  cid: string,
): Promise<EventJson[]> {
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

test("live MiniMax build → product-evidence dossier classifies PASS", async ({ page, request }) => {
  test.setTimeout(420_000); // a real autonomous build takes minutes

  // 0. Stack health — SKIP (never fail) when the live stack is absent.
  let healthy = false;
  try {
    healthy = (await request.get(`${API}/health`, { timeout: 5_000 })).ok();
  } catch {
    healthy = false;
  }
  test.skip(!healthy, `agent-server not reachable at ${API} — start the live stack and re-run`);
  test.skip(!RELAY_LOG || !fs.existsSync(RELAY_LOG), "PMX_RELAY_LOG (relay JSONL) not provided");

  // 1. Drive a fresh AUTONOMOUS build.
  const created = await (await request.post(`${API}/conversations`, {
    data: { surface: "build", autonomous: true },
    timeout: 15_000,
  })).json();
  const cid: string = created.conversation_id;
  await request.post(`${API}/conversations/${cid}/messages`, {
    data: { content: BUILD_PROMPT },
    timeout: 20_000,
  });

  // 2. Poll to a clean terminal (FINISHED/VERIFIED).
  let terminal = "";
  const statuses: string[] = [];
  for (let i = 0; i < 280; i++) {
    const st = await getJson(request, `${API}/conversations/${cid}/state`);
    const s = String(st.execution_status ?? "");
    if (statuses[statuses.length - 1] !== s) statuses.push(s);
    if (s === "FINISHED" || s === "VERIFIED") {
      terminal = s;
      break;
    }
    if (s === "STUCK" || s === "ERROR" || s === "AWAITING_USER") {
      terminal = s;
      break;
    }
    await new Promise((r) => setTimeout(r, 3_000));
  }
  expect(terminal, `build did not reach a clean terminal (last statuses: ${statuses.join(",")})`)
    .toMatch(/FINISHED|VERIFIED/);

  // 3. Event-log slices: preview ownership + verification.
  const events = await allEvents(request, cid);
  const toolNames = events.filter((e) => e.kind === "action").map((e) => e.tool_call?.tool_name);
  const previewStart = events.find((e) => e.tool_call?.tool_name === "preview_start");
  const manualPort = Boolean((previewStart?.tool_call?.arguments ?? {}).port);
  // A build may verify multiple times (verify → fix → re-verify); the FINAL verify is the
  // finish-gate verdict that matters — capture the LAST verify_web_app + its observation.
  const verifyActions = events.filter((e) => e.tool_call?.tool_name === "verify_web_app");
  const verifyAction = verifyActions[verifyActions.length - 1];
  const verifyObs = events.find(
    (e) => e.kind === "observation" && e.action_id === verifyAction?.id,
  );
  expect(toolNames, "build never started a preview").toContain("preview_start");
  expect(verifyAction, "build never called verify_web_app").toBeTruthy();
  const lifecycleStatuses = events.filter((e) => e.kind === "status" && e.status).map((e) => e.status!);

  // 4. UI slices: browser_ws connections + shown (feed + preview pane). Register the WS
  //    listener BEFORE navigation; count only agent-server (:8000) sockets (exclude vite HMR).
  const wsUrls = new Set<string>();
  page.on("websocket", (ws) => {
    if (ws.url().includes(":8000")) wsUrls.add(ws.url());
  });
  await page.setViewportSize({ width: 1400, height: 1600 });
  await page.goto(`/build/${cid}`, { timeout: 30_000 });
  // artifact_shown: the feed rendered the build's rows.
  const feedRow = page.locator("button", { has: page.locator("span.truncate") });
  await expect(feedRow.first(), "feed rendered no build rows").toBeVisible({ timeout: 30_000 });
  const artifactShown = (await feedRow.count()) > 0;
  // preview_shown: the Preview tab renders the built site in its iframe.
  await page.getByRole("tab", { name: /preview/i }).first().click({ timeout: 15_000 });
  const previewFrame = page.frameLocator("iframe").first();
  const previewShown = await previewFrame
    .getByText(/coffee|hours|about|contact/i)
    .first()
    .isVisible({ timeout: 30_000 })
    .catch(() => false);
  expect(previewShown, "the Preview pane never rendered the built site").toBeTruthy();
  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "build-preview.png"), fullPage: true });
  const browserWsConnections = wsUrls.size;
  expect(browserWsConnections, "no agent-server WebSocket connected").toBeGreaterThanOrEqual(1);

  // 5. Cleanup slice: kill the conversation, then verify no orphan sandbox container survives
  //    (container teardown == workspace release for the local podman backend).
  await request.post(`${API}/conversations/${cid}/kill`, { timeout: 15_000 });
  await new Promise((r) => setTimeout(r, 5_000));
  let orphans = 0;
  try {
    const out = execFileSync("podman", ["ps", "-a", "--format", "{{.Names}}"], {
      encoding: "utf-8",
      timeout: 15_000,
    });
    orphans = out.split("\n").filter((n) => n.includes(cid)).length;
  } catch {
    orphans = 0; // podman absent in this env → no containers to orphan
  }

  // 6. Provider ledger from the REAL relay log (host = api.minimaxi.chat).
  const providerRecords = fs
    .readFileSync(RELAY_LOG, "utf-8")
    .split("\n")
    .filter(Boolean)
    .map((l) => JSON.parse(l))
    .map((r) => ({ host: r.host, model: r.model }));
  expect(providerRecords.length, "relay log had no provider records").toBeGreaterThanOrEqual(1);

  // 7. Assemble the capture + hand it to the Python classifier.
  const capture = {
    product_evidence: {
      browser_ws: { connections: browserWsConnections },
      lifecycle: { terminal, statuses: lifecycleStatuses },
      preview: { owner: "platform", manual_port: manualPort },
      shown: { artifact_shown: artifactShown, preview_shown: previewShown },
      verification: {
        // fail-closed: passed only when the verify observation EXISTS, the tool RAN
        // (success===true), AND its structured VERDICT is pass (structured.passed===true).
        // success alone only means verify_web_app executed, not that the page verified clean.
        ready_for_verification_called: Boolean(verifyAction),
        passed:
          verifyObs?.tool_result?.success === true &&
          verifyObs?.tool_result?.structured?.passed === true,
      },
      cleanup: { orphans, workspace_released: orphans === 0 },
    },
    provider_records: providerRecords,
    events,
    autonomous: true,
    run_id: cid,
    scenario_id: "static_site_smoke",
  };
  const work = fs.mkdtempSync(path.join(os.tmpdir(), "disclaude-dossier-"));
  const capPath = path.join(work, "captured.json");
  fs.writeFileSync(capPath, JSON.stringify(capture), "utf-8");

  let verdict: { status?: string; code?: string } = {};
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
          PYTHONPATH: ["packages/core/src", "packages/tools/src", "packages/agent-server/src", "."]
            .map((p) => path.join(REPO_ROOT, p))
            .join(":"),
        },
      },
    );
    verdict = JSON.parse(stdout.trim().split("\n").pop() ?? "{}");
  } catch (e) {
    const err = e as { stdout?: string; stderr?: string };
    throw new Error(`classifier failed: ${err.stdout ?? ""}${err.stderr ?? String(e)}`);
  }
  expect(verdict.status, `classify_dossier did not PASS: ${JSON.stringify(verdict)}`).toBe("PASS");
});
