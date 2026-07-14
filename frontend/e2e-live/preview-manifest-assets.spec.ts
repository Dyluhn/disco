import {
  expect,
  request as playwrightRequest,
  test,
  type APIRequestContext,
  type BrowserContext,
  type Page,
  type TestInfo,
} from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";

import {
  AGENT_API,
  allEvents,
  assertNoThrash,
  authenticatedMutation,
  deleteConversation,
  inspectTrace,
  requireReliabilityStack,
  restartReliabilityStack,
  type EventJson,
} from "./reliability-helpers";

type Surface = "build" | "agent";

const RELEASE_ENTRY = "release/index.html";
const ROOT_STALE = "STALE ROOT MUST NEVER OPEN";
const FIRST_MARKER = "SELECTED RELEASE ONE";
const SECOND_MARKER = "SELECTED RELEASE TWO";
const SCRIPT_MARKER = "SCRIPT ASSET LOADED";
const NESTED_MARKER = "NESTED ROUTE LOADED";
const BACKGROUND = "rgb(12, 34, 56)";
const FONT_PATH = path.resolve(
  process.cwd(),
  "node_modules/@fontsource-variable/jetbrains-mono/files/jetbrains-mono-cyrillic-ext-wght-normal.woff2",
);
const FONT_BASE64 = fs.readFileSync(FONT_PATH).toString("base64");

function buildPrompt(surface: Surface): string {
  return `
This is a strict ${surface} reliability verification. Do not ask questions and do not
substitute a one-file app. Create these exact files in the workspace:

1. Root index.html containing only the visible text '${ROOT_STALE}'. It is an
   intentionally stale scaffold and must never be served or selected.
2. release/index.html containing a normal HTML document with visible h1 text
   '${FIRST_MARKER}', a Cyrillic Ж character inside an element with id font-proof,
   a stylesheet link href './assets/theme.css?theme=7', a deferred script src
   './scripts/app.js?mode=live', an img id hero src
   './media/hero%20image.svg?asset=1#hero', and an anchor id nested-link href
   './docs/?view=full#nested'. Do not inline any CSS, JavaScript, image, or font.
3. release/assets/theme.css defining @font-face named ProofFont from
   url('../fonts/proof.woff2?font=1#proof') format('woff2'), applying ProofFont
   to #font-proof, and setting body background to ${BACKGROUND}.
4. release/scripts/app.js setting document.body.dataset.scriptLoaded to 'true'
   and appending visible text '${SCRIPT_MARKER}'.
5. release/media/hero image.svg as a valid SVG with visible text 'SVG ASSET'.
6. release/docs/index.html as a normal HTML document containing visible text
   '${NESTED_MARKER}'.
7. release/fonts/proof.woff2 by decoding this base64 with the shell, without
   printing it: ${FONT_BASE64}

Use ordinary workspace write/shell tools. Verify the files and links. Then call the
serve tool exactly once with path '${RELEASE_ENTRY}' (the entry FILE, not release,
dot, or the workspace root), title 'Selected reliability release', kind 'app', and finish.
`;
}

async function createRun(
  request: APIRequestContext,
  surface: Surface,
): Promise<string> {
  const create = await authenticatedMutation(request, `${AGENT_API}/conversations`, {
    data: {
      surface,
      title: `${surface} manifest asset reliability`,
      autonomous: surface === "agent",
      assist: false,
    },
  });
  expect(create.ok(), await create.text()).toBe(true);
  const cid = String((await create.json()).conversation_id);
  const kick = await authenticatedMutation(
    request,
    `${AGENT_API}/conversations/${cid}/messages`,
    { data: { content: buildPrompt(surface) }, timeout: 30_000 },
  );
  expect(kick.ok(), await kick.text()).toBe(true);
  return cid;
}

async function approveBuildPlan(page: Page): Promise<void> {
  const approve = page.locator('[data-disco-control="approve-plan"]').first();
  const backendError = page.getByRole("heading", { name: "Can't reach the server" });
  await Promise.race([
    approve.waitFor({ state: "visible", timeout: 300_000 }),
    backendError.waitFor({ state: "visible", timeout: 300_000 }).then(async () => {
      throw new Error(`Build surface lost its backend: ${await page.locator("body").innerText()}`);
    }),
  ]);
  await approve.click();
}

async function waitForCommittedFinish(
  request: APIRequestContext,
  cid: string,
  afterSeq = 0,
): Promise<{ version: number; events: EventJson[] }> {
  const deadline = Date.now() + 900_000;
  let latest: EventJson[] = [];
  while (Date.now() < deadline) {
    latest = await allEvents(request, cid);
    const terminal = latest.find(
      (event) =>
        (event.seq ?? 0) > afterSeq &&
        event.kind === "status" &&
        new Set(["ERROR", "STUCK"]).has(String(event.status)),
    );
    if (terminal) {
      throw new Error(`conversation ${cid} reached ${terminal.status}: ${String(terminal.detail ?? "")}`);
    }
    const finished = [...latest]
      .reverse()
      .find(
        (event) =>
          (event.seq ?? 0) > afterSeq && event.kind === "status" && event.status === "FINISHED",
      );
    if (finished) {
      const committed = latest.find(
        (event) => event.kind === "workspace_version" && (event.seq ?? 0) > (finished.seq ?? 0),
      );
      if (committed) {
        const version = Number(committed.version_seq);
        expect(version, "workspace-version event has no numeric version_seq").toBeGreaterThan(0);
        return { version, events: latest };
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
  throw new Error(`conversation ${cid} did not finish with a durable version after seq ${afterSeq}`);
}

function selectedApp(events: EventJson[]): EventJson | undefined {
  return [...events]
    .reverse()
    .find((event) => event.kind === "deliverable" && event.artifact_kind === "app");
}

function verifierEvidence(events: EventJson[]): {
  model_judge_invoked: boolean;
  events: Array<Record<string, unknown>>;
} {
  const verifierEvents = events.filter((event) =>
    new Set(["verifier_started", "verifier_shadow", "verifier_verdict"]).has(String(event.kind)),
  );
  expect(verifierEvents.length, "finish emitted no host-verifier evidence").toBeGreaterThan(0);
  const annotated = verifierEvents.flatMap((event) => {
    if (!new Set(["verifier_shadow", "verifier_verdict"]).has(String(event.kind))) return [];
    const meta = event.meta;
    if (!meta || typeof meta !== "object" || Array.isArray(meta)) return [];
    const record = meta as Record<string, unknown>;
    const modelKeys = [
      "model_verifier_status",
      "model_verifier_applied",
      "model_verifier_cause",
    ].filter((key) => record[key] !== undefined);
    if (modelKeys.length === 0) return [];
    expect(typeof record.model_verifier_status).toBe("string");
    expect(typeof record.model_verifier_applied).toBe("boolean");
    if (record.model_verifier_applied === false) {
      expect(typeof record.model_verifier_cause).toBe("string");
      expect(String(record.model_verifier_cause).length).toBeLessThanOrEqual(256);
      expect(String(record.model_verifier_cause)).not.toMatch(/authorization|api[_-]?key|secret/i);
    }
    return [{
      kind: event.kind,
      seq: event.seq,
      model_verifier_status: record.model_verifier_status,
      model_verifier_applied: record.model_verifier_applied,
      ...(record.model_verifier_cause === undefined
        ? {}
        : { model_verifier_cause: record.model_verifier_cause }),
    }];
  });
  return { model_judge_invoked: annotated.length > 0, events: annotated };
}

async function assertApiGraph(
  request: APIRequestContext,
  cid: string,
  marker: string,
  version?: number,
): Promise<void> {
  const suffix = version === undefined ? "" : `?version=${version}`;
  const base = `${AGENT_API}/conversations/${cid}/preview-app`;
  const root = await request.get(`${base}/${suffix}`);
  expect(root.ok(), await root.text()).toBe(true);
  const rootBody = await root.text();
  expect(rootBody).toContain(marker);
  expect(rootBody).not.toContain(ROOT_STALE);

  const css = await request.get(`${base}/assets/theme.css?theme=7`);
  const prefixedCss = await request.get(`${base}/release/assets/theme.css?theme=7`);
  const script = await request.get(`${base}/scripts/app.js?mode=live`);
  const doubleSlashScript = await request.get(`${base}//scripts/app.js?mode=live`);
  const image = await request.get(`${base}/media/hero%20image.svg?asset=1`);
  const font = await request.get(`${base}/fonts/proof.woff2?font=1`);
  const nested = await request.get(`${base}/docs/?view=full#nested`);
  for (const response of [css, prefixedCss, script, doubleSlashScript, image, font, nested]) {
    expect(response.ok(), `${response.url()} -> ${response.status()}: ${await response.text()}`).toBe(
      true,
    );
  }
  expect(await css.text()).toContain("ProofFont");
  expect(await prefixedCss.text()).toBe(await css.text());
  expect(await script.text()).toContain(SCRIPT_MARKER);
  expect(await doubleSlashScript.text()).toBe(await script.text());
  expect(await image.text()).toContain("SVG ASSET");
  expect((await font.body()).byteLength).toBeGreaterThan(1_000);
  expect(await nested.text()).toContain(NESTED_MARKER);

  const traversal = await request.get(`${base}/%2e%2e/%2e%2e/etc/passwd`);
  expect(traversal.ok()).toBe(false);
  expect(await traversal.text()).not.toContain("root:");
  const backslash = await request.get(`${base}/assets%5Ctheme.css`);
  expect(backslash.ok()).toBe(false);

  const anonymous = await playwrightRequest.newContext();
  try {
    const unauthenticated = await anonymous.get(`${base}/${suffix}`);
    expect([401, 403]).toContain(unauthenticated.status());
    expect(await unauthenticated.text()).not.toContain(marker);
  } finally {
    await anonymous.dispose();
  }
}

async function assertRenderedDocument(page: Page, marker: string): Promise<void> {
  const body = page.locator("body");
  await expect(body).toContainText(marker, { timeout: 120_000 });
  await expect(body).toHaveAttribute("data-script-loaded", "true", { timeout: 120_000 });
  await expect(body).toContainText(SCRIPT_MARKER);
  expect(await body.evaluate((element) => getComputedStyle(element).backgroundColor)).toBe(BACKGROUND);
  await expect
    .poll(() => page.locator("#hero").evaluate((image: HTMLImageElement) => image.naturalWidth > 0), {
      timeout: 120_000,
    })
    .toBe(true);
  expect(
    await body.evaluate(async (element) => {
      await element.ownerDocument.fonts.ready;
      return element.ownerDocument.fonts.check("16px ProofFont", "Ж");
    }),
  ).toBe(true);
}

async function openFromHandoff(
  context: BrowserContext,
  page: Page,
  cid: string,
  marker: string,
): Promise<void> {
  const open = page.locator('[data-disco-control="build.open-app"]').first();
  await expect(open).toBeVisible({ timeout: 120_000 });
  // The handoff control renders from the durable DeliverableEvent before the
  // isolated capability POST resolves. This is especially visible after a
  // cold stack restart; synchronize on the signed attribute we consume rather
  // than assuming control visibility implies async mint completion.
  await expect(open).toHaveAttribute("data-app-url", /.+/, { timeout: 120_000 });
  const handoffUrl = await open.getAttribute("data-app-url");
  expect(handoffUrl, "handoff did not mint an isolated preview capability").toBeTruthy();
  const parsedHandoff = new URL(handoffUrl!);
  expect(parsedHandoff.pathname).toBe(
    `/__disco/path-preview-auth/${cid.replace(/^conv_/, "").slice(0, 8)}`,
  );
  expect(parsedHandoff.searchParams.get("intent")).toBeTruthy();
  expect(parsedHandoff.origin).not.toBe(new URL(page.url()).origin);
  const popupPromise = context.waitForEvent("page", { timeout: 30_000 });
  await open.click();
  const popup = await popupPromise;
  try {
    await popup.waitForLoadState("domcontentloaded");
    await assertRenderedDocument(popup, marker);
    await popup.reload();
    await assertRenderedDocument(popup, marker);
    await popup.locator("#nested-link").click();
    await expect(popup.locator("body")).toContainText(NESTED_MARKER, { timeout: 60_000 });
    expect(popup.url()).toMatch(/\/preview-app\/docs\/?\?view=full#nested$/);
  } finally {
    await popup.close();
  }
}

async function assertBuildIframe(
  page: Page,
  cid: string,
  marker: string,
): Promise<string[]> {
  const responses: string[] = [];
  const listener = (response: { url(): string; ok(): boolean }) => {
    if (response.url().includes(`/conversations/${cid}/preview-app/`) && response.ok()) {
      responses.push(response.url());
    }
  };
  page.on("response", listener);
  try {
    await page.getByRole("tab", { name: "Preview" }).click();
    const frame = page.frameLocator('iframe[title="Static preview"]').first();
    const body = frame.locator("body");
    await expect(body).toContainText(marker, { timeout: 120_000 });
    await expect(body).toHaveAttribute("data-script-loaded", "true", { timeout: 120_000 });
    expect(await body.evaluate((element) => getComputedStyle(element).backgroundColor)).toBe(BACKGROUND);
    await expect
      .poll(
        () => frame.locator("#hero").evaluate((image: HTMLImageElement) => image.naturalWidth > 0),
        { timeout: 120_000 },
      )
      .toBe(true);
    expect(
      await body.evaluate(async (element) => {
        await element.ownerDocument.fonts.ready;
        return element.ownerDocument.fonts.check("16px ProofFont", "Ж");
      }),
    ).toBe(true);
    // A finished Build auto-selects Preview, so this iframe can complete its
    // graph before this helper is reached (the popup handoff runs first). A
    // page response listener is prospective only; merge it with the frame's
    // own same-origin Resource Timing history instead of treating an empty
    // listener as proof that earlier requests never happened.
    const completedResources = await body.evaluate((element) =>
      (element.ownerDocument.defaultView?.performance.getEntriesByType("resource") ?? []).map(
        (entry) => entry.name,
      ),
    );
    const observedResponses = [...new Set([...responses, ...completedResources])];
    for (const expected of [
      "/assets/theme.css?theme=7",
      "/scripts/app.js?mode=live",
      "/media/hero%20image.svg?asset=1",
      "/fonts/proof.woff2?font=1",
    ]) {
      expect(
        observedResponses.some((url) => url.includes(expected)),
        `iframe never loaded ${expected}`,
      ).toBe(true);
    }
    return observedResponses;
  } finally {
    page.off("response", listener);
  }
}

async function waitForStack(request: APIRequestContext): Promise<void> {
  await expect
    .poll(
      async () => {
        try {
          return (await request.get(`${AGENT_API}/health`, { timeout: 5_000 })).ok();
        } catch {
          return false;
        }
      },
      { timeout: 120_000, intervals: [500, 1_000, 2_000] },
    )
    .toBe(true);
  await requireReliabilityStack(request);
}

test("Build and Agent open the selected multi-file manifest across restart and revision", async ({
  context,
  page,
  request,
}, testInfo: TestInfo) => {
  test.setTimeout(2_700_000);
  await requireReliabilityStack(request);

  const consoleErrors: string[] = [];
  const failedPreviewRequests: string[] = [];
  const observe = (candidate: Page) => {
    candidate.on("console", (message) => {
      if (message.type() === "error") consoleErrors.push(message.text());
    });
    candidate.on("requestfailed", (failed) => {
      if (failed.url().includes("/preview-app/")) {
        failedPreviewRequests.push(`${failed.url()}: ${failed.failure()?.errorText ?? "unknown"}`);
      }
    });
  };
  observe(page);
  context.on("page", observe);

  const cids: string[] = [];
  try {
    const buildCid = await createRun(request, "build");
    cids.push(buildCid);
    await page.goto(`/build/${buildCid}`);
    await approveBuildPlan(page);
    const buildFirst = await waitForCommittedFinish(request, buildCid);
    const buildFirstVerifier = verifierEvidence(buildFirst.events);
    expect(selectedApp(buildFirst.events)?.path).toBe(RELEASE_ENTRY);
    assertNoThrash(buildFirst.events, await inspectTrace(request, buildCid));
    await assertApiGraph(request, buildCid, FIRST_MARKER);
    await openFromHandoff(context, page, buildCid, FIRST_MARKER);
    const iframeResponses = await assertBuildIframe(page, buildCid, FIRST_MARKER);

    const agentPage = await context.newPage();
    const agentCid = await createRun(request, "agent");
    cids.push(agentCid);
    await agentPage.goto(`/agent/${agentCid}`);
    const agentFirst = await waitForCommittedFinish(request, agentCid);
    const agentFirstVerifier = verifierEvidence(agentFirst.events);
    expect(selectedApp(agentFirst.events)?.path).toBe(RELEASE_ENTRY);
    assertNoThrash(agentFirst.events, await inspectTrace(request, agentCid));
    await assertApiGraph(request, agentCid, FIRST_MARKER);
    await openFromHandoff(context, agentPage, agentCid, FIRST_MARKER);

    await restartReliabilityStack();
    await waitForStack(request);
    await assertApiGraph(request, buildCid, FIRST_MARKER);
    await assertApiGraph(request, agentCid, FIRST_MARKER);
    await page.reload();
    await openFromHandoff(context, page, buildCid, FIRST_MARKER);
    await agentPage.reload();
    await openFromHandoff(context, agentPage, agentCid, FIRST_MARKER);

    const beforeRevision = Math.max(0, ...buildFirst.events.map((event) => event.seq ?? 0));
    const revise = await authenticatedMutation(
      request,
      `${AGENT_API}/conversations/${buildCid}/messages`,
      {
        data: {
          content:
            `Revise only the selected release. Change the visible h1 in ${RELEASE_ENTRY} ` +
            `from '${FIRST_MARKER}' to exactly '${SECOND_MARKER}'. Keep every external asset ` +
            `and route intact, serve exactly '${RELEASE_ENTRY}', verify, and finish. Never serve ` +
            "the stale root index.html.",
        },
      },
    );
    expect(revise.ok(), await revise.text()).toBe(true);
    await page.reload();
    await approveBuildPlan(page);
    const buildSecond = await waitForCommittedFinish(request, buildCid, beforeRevision);
    const buildSecondVerifier = verifierEvidence(buildSecond.events);
    expect(selectedApp(buildSecond.events)?.path).toBe(RELEASE_ENTRY);
    expect(buildSecond.version).toBeGreaterThan(buildFirst.version);
    assertNoThrash(buildSecond.events, await inspectTrace(request, buildCid));
    await assertApiGraph(request, buildCid, SECOND_MARKER);
    const current = await (await request.get(`${AGENT_API}/conversations/${buildCid}/preview-app/`)).text();
    expect(current).not.toContain(FIRST_MARKER);
    await assertApiGraph(request, buildCid, FIRST_MARKER, buildFirst.version);
    await openFromHandoff(context, page, buildCid, SECOND_MARKER);

    expect(consoleErrors, `browser console errors: ${JSON.stringify(consoleErrors)}`).toEqual([]);
    expect(
      failedPreviewRequests,
      `failed preview requests: ${JSON.stringify(failedPreviewRequests)}`,
    ).toEqual([]);

    const evidence = {
      revision: process.env.DISCO_RELIABILITY_REVISION ?? null,
      build: {
        cid: buildCid,
        first_version: buildFirst.version,
        second_version: buildSecond.version,
        selected_entry: selectedApp(buildSecond.events)?.path,
        event_count: buildSecond.events.length,
        first_verifier: buildFirstVerifier,
        second_verifier: buildSecondVerifier,
      },
      agent: {
        cid: agentCid,
        version: agentFirst.version,
        selected_entry: selectedApp(agentFirst.events)?.path,
        event_count: agentFirst.events.length,
        verifier: agentFirstVerifier,
      },
      iframe_responses: iframeResponses,
      console_errors: consoleErrors,
      failed_preview_requests: failedPreviewRequests,
    };
    fs.writeFileSync(
      testInfo.outputPath("preview-manifest-assets-evidence.json"),
      JSON.stringify(evidence, null, 2),
      "utf-8",
    );
  } finally {
    context.removeAllListeners("page");
    for (const cid of cids.reverse()) await deleteConversation(request, cid).catch(() => undefined);
  }
});
