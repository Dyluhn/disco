import { expect, test } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

// ESM module scope — no __dirname; derive it (frontend package is "type": "module").
const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * BP-11 UI rung (live, DRIVES A BUILD): the behavioral scenario through the real
 * UI. Upload a REAL csv via the composer paperclip (setInputFiles), assert the
 * toast + the feed announcement line, prompt a build that renders the csv as a
 * table, approve the plan, and on FINISH assert the Files tab lists uploads/ and
 * the Preview renders REAL cell values.
 *
 * Evidence: test-record/screenshots/bp-11/upload-toast.png + preview-table.png
 *           test-record/bp-11/upload-ui-witness.json
 *
 * The csv mirrors test-record/bp-11/run_behavioral.py: the campaign's own bp-00
 * behavioral-run records (real data, not lorem ipsum).
 */

const API = "http://127.0.0.1:8000";
const SCREENSHOT_DIR = path.resolve(__dirname, "../../test-record/screenshots/bp-11");
const RECORD_DIR = path.resolve(__dirname, "../../test-record/bp-11");

const CSV_NAME = "bp00-behavioral-runs.csv";
const CSV_BODY = [
  "run,conversation,final_status,duration_s,events,screenshot_obs",
  "2,conv_3b9402aa654b4a43b715db10ce7b6550,ERROR,42,29,0",
  "3,conv_bc8596a9ac3f43b484aaa47ad8b83372,FINISHED,375,109,15",
  "4,conv_2927ed545aa347069428766a8514c8fa,FINISHED,105,33,2",
  "5,conv_c5f4f81c22624a2fac545769f6b300e9,FINISHED,135,28,2",
].join("\n");
// Distinct cell values from different rows (not headers).
const CELL_A = "conv_bc8596a9ac3f43b484aaa47ad8b83372";
const CELL_B = "135";

const PROMPT =
  "I've uploaded a CSV of test-run records. Build a single page that renders the " +
  "uploaded CSV as an HTML table — every row and column, real values, no " +
  "placeholders. Serve it, check it looks right, then finish.";

test("upload → announce → build renders the real csv as a table (live)", async ({
  page,
  request,
}) => {
  test.setTimeout(900_000); // live build on the local 27B takes minutes

  // 1. Backend health — skip, never crash, when the agent-server is down.
  let healthy = false;
  try {
    healthy = (await request.get(`${API}/health`, { timeout: 5_000 })).ok();
  } catch {
    healthy = false;
  }
  test.skip(!healthy, `agent-server not reachable at ${API} — start it and re-run`);

  // 2. Fresh conversation (API-created so the paperclip has a cid to post to).
  const conv = await (
    await request.post(`${API}/conversations`, {
      data: { surface: "build", title: "BP-11 UI rung: upload + table build" },
    })
  ).json();
  const cid: string = conv.conversation_id;

  await page.setViewportSize({ width: 1280, height: 2200 }); // bp-04 lesson: no autoscroll under shots
  await page.goto(`/build/${cid}`, { timeout: 30_000 });

  // 3. Upload via the composer paperclip — the hidden <input type="file">.
  fs.mkdirSync(RECORD_DIR, { recursive: true });
  const csvPath = path.join(RECORD_DIR, CSV_NAME);
  fs.writeFileSync(csvPath, CSV_BODY);
  await page.setInputFiles('input[type="file"]', csvPath, { timeout: 15_000 });

  // Toast: "Uploaded 1 file(s) to uploads/" (exact copy owned by the worker —
  // match the stable parts).
  const toast = page.getByText(/uploaded .*uploads\//i).first();
  await expect(toast, "upload success toast did not appear").toBeVisible({ timeout: 15_000 });
  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "upload-toast.png") });

  // Feed announcement: the ONE environment MessageEvent.
  const announce = page.getByText(new RegExp(`User uploaded: uploads/${CSV_NAME}`)).first();
  await expect(announce, "feed announcement line missing").toBeVisible({ timeout: 30_000 });

  // 4. Prompt the build and approve the plan.
  // /build/{cid} is the RESUME path (useBuild.ts:23-26 → started=true), so the
  // surface renders the chat-pane composer ("Plan a change to this build…" →
  // requestPlan WS frame), NOT the front-door one (which creates its OWN cid).
  const composer = page.getByPlaceholder(/plan a change to this build/i);
  await composer.fill(PROMPT, { timeout: 30_000 });
  await composer.press("Enter");

  const approve = page.getByRole("button", { name: /approve\s*&\s*build/i });
  await approve.click({ timeout: 300_000 });

  // 5. Wait for FINISH (poll state via API — steadier than UI text alone).
  const deadline = Date.now() + 600_000;
  let status = "?";
  while (Date.now() < deadline) {
    status = (await (await request.get(`${API}/conversations/${cid}/state`)).json())
      .execution_status;
    if (["FINISHED", "ERROR"].includes(status)) break;
    await new Promise((r) => setTimeout(r, 5_000));
  }
  expect(status, `run ended ${status}`).toBe("FINISHED");
  await expect(page.getByText("Finished", { exact: true }).first()).toBeVisible({
    timeout: 30_000,
  });

  // 6. Files tab lists the upload.
  await page.getByRole("tab", { name: /files/i }).first().click();
  await expect(
    page.getByText(new RegExp(`uploads(/|\\b)`)).first(),
    "Files tab does not show the uploads/ entry",
  ).toBeVisible({ timeout: 30_000 });

  // 7. Preview renders the REAL table (two cells from different rows).
  await page.getByRole("tab", { name: /preview/i }).first().click();
  const frame = page.frameLocator("iframe").first();
  await expect(frame.getByText(CELL_A).first(), "cell A missing from preview").toBeVisible({
    timeout: 30_000,
  });
  await expect(frame.getByText(CELL_B, { exact: true }).first(), "cell B missing").toBeVisible({
    timeout: 30_000,
  });
  await page.screenshot({ path: path.join(SCREENSHOT_DIR, "preview-table.png") });

  fs.writeFileSync(
    path.join(RECORD_DIR, "upload-ui-witness.json"),
    JSON.stringify({ cid, csv: CSV_NAME, cells: [CELL_A, CELL_B], status }, null, 2),
  );
});
