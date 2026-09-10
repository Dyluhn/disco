import { expect, test } from "@playwright/test";

/**
 * BP-10 now protects the one-Preview contract. A build may bind several USER
 * ports internally, but the platform-selected application is the only ordinary
 * user-facing Preview. Alternate services remain runtime implementation detail;
 * there is no Rendered/Live switch or port-picker competing with the app handoff.
 */

const API = "http://127.0.0.1:8000";

test("multiple bound ports still expose one canonical real Preview (live)", async ({
  page,
  request,
}) => {
  test.setTimeout(900_000);

  let healthy = false;
  try {
    healthy = (await request.get(`${API}/health`, { timeout: 5_000 })).ok();
  } catch {
    healthy = false;
  }
  test.skip(!healthy, `agent-server not reachable at ${API} — start it and re-run`);

  const cid = process.env.PMX_SPEC_CID ?? "";
  test.skip(!cid, "set PMX_SPEC_CID to the running BP-10 behavioral conversation");

  let preview: Record<string, unknown> = {};
  const deadline = Date.now() + 600_000;
  while (Date.now() < deadline) {
    preview = await (await request.get(`${API}/conversations/${cid}/preview`)).json();
    if (preview.available === true && typeof preview.generation === "string") break;
    const state = await (await request.get(`${API}/conversations/${cid}/state`)).json();
    if (["FINISHED", "ERROR"].includes(state.execution_status)) break;
    await new Promise((resolve) => setTimeout(resolve, 4_000));
  }
  test.skip(preview.available !== true, "the platform-selected Preview never became ready");

  await page.goto(`/build/${cid}`, { timeout: 30_000 });
  await page.getByRole("tab", { name: /preview/i }).first().click();

  await expect(page.getByRole("button", { name: /rendered/i })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /live server/i })).toHaveCount(0);
  await expect(page.getByRole("tablist", { name: "Bound ports" })).toHaveCount(0);

  const frame = page.locator('iframe[title="Preview"]');
  await expect(frame).toBeVisible({ timeout: 30_000 });
  await expect(
    page.frameLocator('iframe[title="Preview"]').getByRole("heading", {
      name: "BP10 multi-port",
    }),
  ).toBeVisible({ timeout: 60_000 });

  const frameUrl =
    page.frames().find((candidate) => candidate.parentFrame() === page.mainFrame())?.url() ?? "";
  expect(frameUrl).toMatch(/^http:\/\/127\.0\.0\.\d+:\d+\//);
  expect(frameUrl).not.toContain(`/conversations/${cid}/port/`);
  expect(typeof preview.generation).toBe("string");
});
