import { expect, test } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

// ESM module scope — no __dirname; derive it (frontend package is "type": "module").
const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * BP-10 UI rung (live, MID-FLIGHT — must run while the build's two servers are up):
 * the PreviewPane renders port pills (role=tablist "Bound ports") ONLY when more than
 * one USER port is bound; selecting :3000 swaps the live iframe to the per-port proxy
 * (/conversations/{cid}/port/3000/). The sandbox is torn down on clean FINISH (the G
 * safe-leak fix), so this spec REQUIRES a pinned, still-running conversation:
 *
 *   PMX_SPEC_CID=<cid of the BP-10 behavioral run, while RUNNING with both ports live>
 *
 * Evidence: test-record/screenshots/bp-10/port-pills-8000.png + port-pills-3000.png +
 *           test-record/bp-10/port-pills-witness.json
 */

const API = "http://127.0.0.1:8000";
const SCREENSHOT_DIR = path.resolve(__dirname, "../../test-record/screenshots/bp-10");
const RECORD_DIR = path.resolve(__dirname, "../../test-record/bp-10");

type PortEntry = { port: number; owner?: { pid?: number; cmdline?: string } | null };

test("port pills switch the live preview between bound USER ports (live)", async ({
  page,
  request,
}) => {
  test.setTimeout(900_000); // includes waiting out the plan gate + build until the window opens

  // 1. Backend health — skip, never crash, when the agent-server is down.
  let healthy = false;
  try {
    const res = await request.get(`${API}/health`, { timeout: 5_000 });
    healthy = res.ok();
  } catch {
    healthy = false;
  }
  test.skip(!healthy, `agent-server not reachable at ${API} — start it and re-run`);

  // 2. Pinned cid is REQUIRED (this spec only makes sense against the live run).
  const cid = process.env.PMX_SPEC_CID ?? "";
  test.skip(!cid, "set PMX_SPEC_CID to the running BP-10 behavioral conversation");

  // 3. WAIT for the mid-flight window: the behavioral run's live window is short
  //    (~70s observed between both-ports-live and FINISH), so this spec is launched
  //    BEFORE the window opens and polls the preview payload until both USER ports
  //    have live owners. A FINISHED conversation can never reach the window
  //    (sandbox torn down) — bail early instead of burning the full poll budget.
  let live = new Set<number>();
  const pollDeadline = Date.now() + 600_000; // covers plan-gate + build time
  while (Date.now() < pollDeadline) {
    const pv = await (await request.get(`${API}/conversations/${cid}/preview`)).json();
    const ports: PortEntry[] = pv.ports ?? [];
    live = new Set(ports.filter((p) => p.owner?.pid).map((p) => p.port));
    if (live.has(8000) && live.has(3000)) break;
    const st = await (await request.get(`${API}/conversations/${cid}/state`)).json();
    if (["FINISHED", "ERROR"].includes(st.execution_status)) break;
    await new Promise((r) => setTimeout(r, 4_000));
  }
  test.skip(
    !(live.has(8000) && live.has(3000)),
    `ports never simultaneously live = [${[...live]}] — need 8000 AND 3000 (run mid-flight)`,
  );

  // 4. Open the build; go to the Preview tab (main canvas tabs, not the pills).
  await page.goto(`/build/${cid}`, { timeout: 30_000 });
  await page.getByRole("tab", { name: /preview/i }).first().click();

  // 5. Pills render only in live-server mode. Which view wins the default is a race:
  //    once index.html exists as an artifact the pane lands in "rendered" mode, where
  //    only a "Live server" toggle shows — and a ONE-SHOT isVisible check right after
  //    the tab click loses to the pane's own data fetch (cost a run: the toggle was
  //    visible in the failure snapshot but hadn't rendered at check time). Wait for
  //    EITHER the pills or the toggle, then keep flipping toward live until pills show.
  const liveBtn = page.getByRole("button", { name: /live server/i });
  const pills = page.getByRole("tablist", { name: "Bound ports" });
  await expect(pills.or(liveBtn).first(), "neither pills nor Live-server toggle appeared")
    .toBeVisible({ timeout: 30_000 });
  for (let i = 0; i < 8 && !(await pills.isVisible().catch(() => false)); i++) {
    if (await liveBtn.isVisible().catch(() => false)) await liveBtn.click();
    await page.waitForTimeout(2_000);
  }
  await expect(pills, "port pills (role=tablist 'Bound ports') not rendered").toBeVisible({
    timeout: 15_000,
  });

  const pill8000 = pills.getByRole("tab", { name: ":8000" });
  const pill3000 = pills.getByRole("tab", { name: ":3000" });
  await expect(pill8000).toBeVisible();
  await expect(pill3000).toBeVisible();
  await expect(pill8000, "primary port should be selected initially").toHaveAttribute(
    "aria-selected",
    "true",
  );

  // 6. Primary (:8000) — the page content actually comes through the proxy.
  const iframe = page.locator('iframe[title="Live preview"]');
  await expect(iframe).toBeVisible();
  await expect(
    page.frameLocator('iframe[title="Live preview"]').locator("h1"),
    "static page heading not served through the preview proxy",
  ).toHaveText("BP10 multi-port", { timeout: 20_000 });
  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "port-pills-8000.png"), fullPage: true });

  // 7. Switch to :3000 — pill selects, iframe swaps to the per-port proxy URL.
  await pill3000.click();
  await expect(pill3000).toHaveAttribute("aria-selected", "true");
  await expect(iframe, "iframe did not swap to the /port/3000/ proxy").toHaveAttribute(
    "src",
    new RegExp(`/conversations/${cid}/port/3000/`),
    { timeout: 10_000 },
  );
  // The JSON service only implements /status.json — prove the data path via the same
  // proxy the iframe uses (its / root may honestly 404; that's the service, not BP-10).
  const statusRes = await request.get(`${API}/conversations/${cid}/port/3000/status.json`);
  expect(statusRes.status(), "/port/3000/status.json not proxied").toBe(200);
  const statusJson = await statusRes.json();
  expect(statusJson.ok, "status service payload wrong").toBe(true);
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "port-pills-3000.png"), fullPage: true });

  // 8. Witness record.
  fs.mkdirSync(RECORD_DIR, { recursive: true });
  fs.writeFileSync(
    path.join(RECORD_DIR, "port-pills-witness.json"),
    JSON.stringify(
      { cid, livePorts: [...live].sort(), status3000: statusJson, pillCount: await pills.getByRole("tab").count() },
      null,
      2,
    ),
  );
});
