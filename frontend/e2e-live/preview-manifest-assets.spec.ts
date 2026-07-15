import {
  expect,
  request as playwrightRequest,
  test,
  type APIRequestContext,
  type BrowserContext,
  type Page,
  type Request,
  type Response,
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
import { latestStoppedStatus } from "@/lib/harness/terminalConversationStatus";

type Surface = "build" | "agent";

// This spec inspects a one-time bearer in memory. Never persist its request or
// response body in a retained Playwright trace when an assertion fails.
test.use({ trace: "off" });

const RELEASE_ENTRY = "release/index.html";
const ROOT_STALE = "STALE ROOT MUST NEVER OPEN";
const RELEASE_TITLE = "RELIABILITY RELEASE";
const FIRST_MARKER = "SELECTED RELEASE ONE";
const SECOND_MARKER = "SELECTED RELEASE TWO";
const SCRIPT_MARKER = "SCRIPT ASSET LOADED";
const SERVICE_WORKER_MARKER = "RELIABILITY SERVICE WORKER";
const NESTED_MARKER = "NESTED ROUTE LOADED";
const ISOLATED_PREVIEW_PREFIX = "/__disco/isolated-preview";
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
2. release/index.html containing a normal HTML document with the exact title element
   '<title>${RELEASE_TITLE}</title>', visible h1 text '${FIRST_MARKER}', a Cyrillic Ж
   character inside an element with id font-proof,
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
7. release/sw.js as valid service-worker JavaScript containing the exact comment
   '/* ${SERVICE_WORKER_MARKER} */' and a no-op install event listener.
8. release/fonts/proof.woff2 by decoding this base64 with the shell, without
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
    const terminal = latestStoppedStatus(latest, afterSeq);
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
  observedBearers: string[],
  version?: number,
  absentMarker?: string,
): Promise<void> {
  const targetPath = version === undefined ? "/" : `/?version=${version}`;
  const mint = await authenticatedMutation(
    request,
    `${AGENT_API}/conversations/${cid}/preview/capability`,
    {
      data: { port: 8000, target_path: targetPath, transport: "path" },
      timeout: 30_000,
    },
  );
  expect(mint.ok(), `preview capability mint returned ${mint.status()}`).toBe(true);
  expect(mint.headers()["cache-control"]).toContain("no-store");
  const capability = (await mint.json()) as {
    bootstrap_url: string;
    bootstrap_intent: string;
    target_path: string;
    transport: string;
  };
  expect(capability.target_path).toBe(targetPath);
  expect(capability.transport).toBe("path");
  expect(
    typeof capability.bootstrap_intent === "string" && capability.bootstrap_intent.length > 0,
    "path capability response omitted its one-time intent",
  ).toBe(true);
  observedBearers.push(capability.bootstrap_intent);

  const bootstrapUrl = new URL(capability.bootstrap_url);
  expect(
    bootstrapUrl.search === "" && bootstrapUrl.hash === "",
    "path bootstrap URL contained query or fragment data",
  ).toBe(true);
  expect(
    !capability.bootstrap_url.includes(capability.bootstrap_intent),
    "path bootstrap URL exposed its one-time intent",
  ).toBe(true);

  const isolated = await playwrightRequest.newContext();
  try {
    const base = `${bootstrapUrl.origin}${ISOLATED_PREVIEW_PREFIX}/${cid}`;
    const withVersion = (path: string): string => {
      if (version === undefined) return `${base}${path}`;
      const hashAt = path.indexOf("#");
      const requestPath = hashAt === -1 ? path : path.slice(0, hashAt);
      const fragment = hashAt === -1 ? "" : path.slice(hashAt);
      const separator = requestPath.includes("?") ? "&" : "?";
      return `${base}${requestPath}${separator}version=${version}${fragment}`;
    };

    const deniedBeforeRedemption = await isolated.get(withVersion("/"));
    expect(deniedBeforeRedemption.status()).toBe(403);
    expect((await deniedBeforeRedemption.text()).includes(marker)).toBe(false);

    const redemption = await isolated.post(capability.bootstrap_url, {
      form: { intent: capability.bootstrap_intent },
      timeout: 30_000,
    });
    expect(redemption.status(), "isolated body-only capability redemption failed").toBe(200);
    expect(
      (await redemption.text()).includes(capability.bootstrap_intent),
      "bootstrap response reflected the one-time bearer",
    ).toBe(false);

    const root = await isolated.get(withVersion("/"));
    expect(root.ok(), `isolated preview root returned ${root.status()}`).toBe(true);
    const rootBody = await root.text();
    expect(rootBody).toContain(marker);
    expect(rootBody).toContain(`<title>${RELEASE_TITLE}</title>`);
    expect(rootBody).not.toContain(ROOT_STALE);
    if (absentMarker) expect(rootBody).not.toContain(absentMarker);

    const css = await isolated.get(withVersion("/assets/theme.css?theme=7"));
    const prefixedCss = await isolated.get(withVersion("/release/assets/theme.css?theme=7"));
    const script = await isolated.get(withVersion("/scripts/app.js?mode=live"));
    const doubleSlashScript = await isolated.get(withVersion("//scripts/app.js?mode=live"));
    const image = await isolated.get(withVersion("/media/hero%20image.svg?asset=1"));
    const font = await isolated.get(withVersion("/fonts/proof.woff2?font=1"));
    const nested = await isolated.get(withVersion("/docs/?view=full#nested"));
    const workerScript = await isolated.get(withVersion("/sw.js"));
    for (const response of [
      css,
      prefixedCss,
      script,
      doubleSlashScript,
      image,
      font,
      nested,
      workerScript,
    ]) {
      expect(
        response.ok(),
        `isolated preview asset ${new URL(response.url()).pathname} returned ${response.status()}`,
      ).toBe(true);
    }
    expect(await css.text()).toContain("ProofFont");
    expect(await prefixedCss.text()).toBe(await css.text());
    expect(await script.text()).toContain(SCRIPT_MARKER);
    expect(await doubleSlashScript.text()).toBe(await script.text());
    expect(await image.text()).toContain("SVG ASSET");
    expect((await font.body()).byteLength).toBeGreaterThan(1_000);
    expect(await nested.text()).toContain(NESTED_MARKER);
    expect(await workerScript.text()).toContain(SERVICE_WORKER_MARKER);

    const traversal = await isolated.get(withVersion("/%2e%2e/%2e%2e/etc/passwd"));
    expect(traversal.ok()).toBe(false);
    expect(await traversal.text()).not.toContain("root:");
    const backslash = await isolated.get(withVersion("/assets%5Ctheme.css"));
    expect(backslash.ok()).toBe(false);
  } finally {
    await isolated.dispose();
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

function requestBelongsToPage(request: Request, page: Page): boolean {
  try {
    return request.frame().page() === page;
  } catch {
    // Playwright requests originating in a service worker have no frame.
    return false;
  }
}

function redactPreviewBearers(value: string, bearers: string[]): string {
  let redacted = value;
  for (const bearer of bearers) {
    redacted = redacted.replaceAll(bearer, "[REDACTED_PREVIEW_BEARER]");
  }
  return redacted.replace(
    /([?&#]intent=)[^&#\s]+/gi,
    "$1[REDACTED_PREVIEW_BEARER]",
  );
}

async function openFromHandoff(
  context: BrowserContext,
  page: Page,
  cid: string,
  marker: string,
  observedBearers: string[],
): Promise<void> {
  const open = page.locator('[data-disco-control="build.open-app"]').first();
  await expect(open).toBeVisible({ timeout: 120_000 });
  await expect(open).not.toHaveAttribute("data-app-url");
  expect(await page.locator("[data-app-url]").count(), "preview bearer exposed in the DOM").toBe(0);

  const compactCid = cid.replace(/^conv_/, "").slice(0, 8);
  const capabilityPathSuffix = `/conversations/${cid}/preview/capability`;
  const bootstrapPath = `/__disco/path-preview-auth/${compactCid}`;
  const intents: string[] = [];

  // SEC004/H112: every gesture opens its popup synchronously, then mints and
  // submits one fresh intent through an ephemeral POST form. Two clicks make
  // cached, eager, or reused capabilities an observable live-test failure.
  for (let click = 0; click < 2; click += 1) {
    const capabilityResponses: Response[] = [];
    const bootstrapRequests: Request[] = [];
    const popupNetworkRequests: Request[] = [];
    const onResponse = (response: Response) => {
      const url = new URL(response.url());
      if (
        response.request().method() === "POST" &&
        url.pathname.endsWith(capabilityPathSuffix)
      ) {
        capabilityResponses.push(response);
      }
    };
    const onRequest = (request: Request) => {
      const url = new URL(request.url());
      if (request.method() === "POST" && url.pathname === bootstrapPath) {
        bootstrapRequests.push(request);
      }
      popupNetworkRequests.push(request);
    };
    context.on("response", onResponse);
    context.on("request", onRequest);

    const popupPromise = context.waitForEvent("page", { timeout: 30_000 });
    await open.click();
    const popup = await popupPromise;
    try {
      await assertRenderedDocument(popup, marker);
      const popupBootstrapRequests = bootstrapRequests.filter((request) =>
        requestBelongsToPage(request, popup),
      );
      expect(
        popupBootstrapRequests.length,
        "each handoff click must submit exactly one isolated bootstrap POST",
      ).toBe(1);
      const bootstrapRequest = popupBootstrapRequests[0];
      const bootstrapUrl = new URL(bootstrapRequest.url());
      expect(bootstrapUrl.pathname === bootstrapPath, "bootstrap path was not exact").toBe(true);
      expect(
        bootstrapUrl.search === "" && bootstrapUrl.hash === "",
        "bootstrap request URL contained query or fragment data",
      ).toBe(true);
      expect(bootstrapRequest.headers()["content-type"]).toContain("application/x-www-form-urlencoded");

      const formBody = new URLSearchParams(bootstrapRequest.postData() ?? "");
      expect([...formBody.keys()]).toEqual(["intent"]);
      const intent = formBody.get("intent");
      expect(intent, "bootstrap POST body omitted its one-time intent").toBeTruthy();
      intents.push(intent!);
      observedBearers.push(intent!);

      expect(
        capabilityResponses.length,
        "each handoff click must mint exactly one fresh preview capability",
      ).toBe(1);
      const capabilityResponse = capabilityResponses[0];
      expect(
        capabilityResponse.ok(),
        `capability mint returned ${capabilityResponse.status()}`,
      ).toBe(true);
      const capabilityRequestUrl = new URL(capabilityResponse.request().url());
      expect(capabilityRequestUrl.pathname.endsWith(capabilityPathSuffix)).toBe(true);
      expect(capabilityRequestUrl.search).toBe("");
      expect(capabilityRequestUrl.hash).toBe("");
      expect(capabilityResponse.request().postDataJSON()).toEqual({
        port: 8000,
        target_path: "/",
        transport: "path",
      });
      expect(capabilityResponse.headers()["cache-control"]).toContain("no-store");
      const capability = (await capabilityResponse.json()) as {
        bootstrap_url: string;
        bootstrap_intent: string;
      };
      expect(
        capability.bootstrap_intent === intent,
        "capability response and bootstrap form used different intents",
      ).toBe(true);
      expect(
        capability.bootstrap_url === bootstrapRequest.url(),
        "minted bootstrap URL did not match submitted form action",
      ).toBe(true);
      const capabilityBootstrapUrl = new URL(capability.bootstrap_url);
      expect(capabilityBootstrapUrl.pathname === bootstrapPath, "minted bootstrap path was not exact").toBe(true);
      expect(
        capabilityBootstrapUrl.search === "" && capabilityBootstrapUrl.hash === "",
        "minted bootstrap URL contained query or fragment data",
      ).toBe(true);
      expect(
        capabilityBootstrapUrl.hostname.match(
          new RegExp(`^p3s-${compactCid}-[0-9a-f]{40}-8000\\.`),
        ),
        "path bootstrap did not use the dedicated per-CID p3s origin",
      ).not.toBeNull();
      expect(capabilityBootstrapUrl.origin).not.toBe(new URL(page.url()).origin);

      const finalUrl = new URL(popup.url());
      expect(
        finalUrl.pathname === `${ISOLATED_PREVIEW_PREFIX}/${cid}/`,
        "popup did not reach the exact signed preview target",
      ).toBe(true);
      expect(
        finalUrl.search === "" && finalUrl.hash === "",
        "final popup URL contained query or fragment data",
      ).toBe(true);
      expect(finalUrl.origin).toBe(capabilityBootstrapUrl.origin);
      expect(popup.url().includes(intent!), "bearer leaked into final popup URL").toBe(false);
      expect(
        bootstrapRequest.url().includes(intent!),
        "bearer leaked into bootstrap request URL",
      ).toBe(false);
      expect(await page.locator('input[name="intent"]').count()).toBe(0);
      expect(await page.locator("[data-app-url]").count()).toBe(0);
      expect(
        (await page.content()).includes(intent!),
        "bearer remained in application DOM",
      ).toBe(false);
      const credentialStatus = await popup.evaluate(async () => {
        const response = await fetch("/api/auth/pairing-token", {
          cache: "no-store",
          credentials: "include",
        });
        return response.status;
      });
      expect(
        credentialStatus,
        "generated preview origin reached a credential endpoint",
      ).toBe(403);
      if (click === 0) {
        const workerResponsePromise = context.waitForEvent("response", {
          predicate: (response) => {
            const responseUrl = new URL(response.url());
            return (
              response.request().method() === "GET" &&
              responseUrl.origin === finalUrl.origin &&
              responseUrl.pathname === "/sw.js"
            );
          },
          timeout: 30_000,
        });
        const workerProbePromise = popup.evaluate(async () => {
          if (!("serviceWorker" in navigator)) {
            return { supported: false, registered: false, registrations: -1 };
          }
          try {
            const registration = await navigator.serviceWorker.register("/sw.js");
            await registration.unregister();
            return {
              supported: true,
              registered: true,
              registrations: (await navigator.serviceWorker.getRegistrations()).length,
            };
          } catch (error) {
            return {
              supported: true,
              registered: false,
              error: error instanceof Error ? error.name : typeof error,
              registrations: (await navigator.serviceWorker.getRegistrations()).length,
            };
          }
        });
        const [workerResponse, workerProbe] = await Promise.all([
          workerResponsePromise,
          workerProbePromise,
        ]);
        expect(workerResponse.status(), "p3s service-worker policy did not return 403").toBe(403);
        expect(workerResponse.headers()["cache-control"]).toContain("no-store");
        expect(await workerResponse.text()).toBe("preview service workers disabled");
        expect(workerProbe.supported, "Firefox did not expose serviceWorker on p3s").toBe(true);
        expect(workerProbe.registered, "p3s accepted a generated service worker").toBe(false);
        expect(workerProbe.registrations, "p3s retained a service-worker registration").toBe(0);
      }
      const requestsFromPopup = popupNetworkRequests.filter((request) =>
        requestBelongsToPage(request, popup),
      );
      expect(requestsFromPopup.length).toBeGreaterThan(0);
      for (const request of requestsFromPopup) {
        const requestUrl = new URL(request.url());
        expect(request.url().includes(intent!), "bearer leaked into popup request URL").toBe(false);
        expect(requestUrl.searchParams.has("intent")).toBe(false);
      }

      // Reload proves the persisted cookie reaches the clean final target;
      // the one-time bearer is absent from address-bar and history-visible URLs.
      await popup.reload();
      await assertRenderedDocument(popup, marker);
      expect(popup.url().includes(intent!), "bearer leaked into reloaded popup URL").toBe(false);
      if (click === 0) {
        await popup.locator("#nested-link").click();
        await expect(popup.locator("body")).toContainText(NESTED_MARKER, {
          timeout: 60_000,
        });
        expect(popup.url().includes(intent!), "bearer leaked into nested-route URL").toBe(false);
        expect(
          new URL(popup.url()).pathname === `${ISOLATED_PREVIEW_PREFIX}/${cid}/docs/` &&
            new URL(popup.url()).search === "?view=full" &&
            new URL(popup.url()).hash === "#nested",
          "popup did not reach the expected nested route",
        ).toBe(true);
      }
    } finally {
      context.off("response", onResponse);
      context.off("request", onRequest);
      await popup.close();
    }
  }

  expect(intents.length).toBe(2);
  expect(
    intents[1] !== intents[0],
    "separate handoff clicks reused a one-time intent",
  ).toBe(true);
}

async function assertCrossCidStorageIsolation(
  context: BrowserContext,
  firstOwnerPage: Page,
  firstCid: string,
  firstMarker: string,
  secondOwnerPage: Page,
  secondCid: string,
  secondMarker: string,
): Promise<void> {
  const open = async (ownerPage: Page, marker: string): Promise<Page> => {
    const popupPromise = context.waitForEvent("page", { timeout: 30_000 });
    await ownerPage.locator('[data-disco-control="build.open-app"]').first().click();
    const popup = await popupPromise;
    await assertRenderedDocument(popup, marker);
    return popup;
  };

  const storageKey = "disco-h137-storage-sentinel";
  const cacheName = "disco-h137-cache-sentinel";
  const cachePath = "/__disco/h137-cache-sentinel";
  const first = await open(firstOwnerPage, firstMarker);
  let second: Page | null = null;
  try {
    await first.evaluate(
      async ({ key, name, requestPath }) => {
        localStorage.setItem(key, "cid-a");
        const cache = await caches.open(name);
        await cache.put(requestPath, new Response("cid-a"));
      },
      { key: storageKey, name: cacheName, requestPath: cachePath },
    );

    // Launch CID B only after CID A has durable state. Under the old shared
    // localhost/127 origin, B's bootstrap Clear-Site-Data erased A here.
    second = await open(secondOwnerPage, secondMarker);
    expect(new URL(first.url()).hostname).toMatch(
      new RegExp(
        `^p3s-${firstCid.replace(/^conv_/, "").slice(0, 8)}-[0-9a-f]{40}-8000\\.`,
      ),
    );
    expect(new URL(second.url()).hostname).toMatch(
      new RegExp(
        `^p3s-${secondCid.replace(/^conv_/, "").slice(0, 8)}-[0-9a-f]{40}-8000\\.`,
      ),
    );
    expect(new URL(first.url()).origin).not.toBe(new URL(second.url()).origin);

    expect(
      await second.evaluate(
        async ({ key, requestPath }) => ({
          local: localStorage.getItem(key),
          cached: await caches.match(requestPath).then((response) => response?.text() ?? null),
        }),
        { key: storageKey, requestPath: cachePath },
      ),
    ).toEqual({ local: null, cached: null });
    expect(
      await first.evaluate(
        async ({ key, requestPath }) => ({
          local: localStorage.getItem(key),
          cached: await caches.match(requestPath).then((response) => response?.text() ?? null),
        }),
        { key: storageKey, requestPath: cachePath },
      ),
    ).toEqual({ local: "cid-a", cached: "cid-a" });
  } finally {
    await Promise.all([first.close(), ...(second === null ? [] : [second.close()])]);
  }
}

async function assertBuildIframe(
  page: Page,
  cid: string,
  marker: string,
): Promise<string[]> {
  const responses: string[] = [];
  const listener = (response: { url(): string; ok(): boolean }) => {
    if (
      response.url().includes(`${ISOLATED_PREVIEW_PREFIX}/${cid}/`) &&
      response.ok()
    ) {
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
  const previewBearers: string[] = [];
  const observe = (candidate: Page) => {
    candidate.on("console", (message) => {
      if (message.type() === "error") consoleErrors.push(message.text());
    });
    candidate.on("requestfailed", (failed) => {
      if (failed.url().includes(`${ISOLATED_PREVIEW_PREFIX}/`)) {
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
    await assertApiGraph(request, buildCid, FIRST_MARKER, previewBearers);
    await openFromHandoff(context, page, buildCid, FIRST_MARKER, previewBearers);
    const iframeResponses = await assertBuildIframe(page, buildCid, FIRST_MARKER);

    const agentPage = await context.newPage();
    const agentCid = await createRun(request, "agent");
    cids.push(agentCid);
    await agentPage.goto(`/agent/${agentCid}`);
    const agentFirst = await waitForCommittedFinish(request, agentCid);
    const agentFirstVerifier = verifierEvidence(agentFirst.events);
    expect(selectedApp(agentFirst.events)?.path).toBe(RELEASE_ENTRY);
    assertNoThrash(agentFirst.events, await inspectTrace(request, agentCid));
    await assertApiGraph(request, agentCid, FIRST_MARKER, previewBearers);
    await openFromHandoff(context, agentPage, agentCid, FIRST_MARKER, previewBearers);
    await assertCrossCidStorageIsolation(
      context,
      page,
      buildCid,
      FIRST_MARKER,
      agentPage,
      agentCid,
      FIRST_MARKER,
    );

    await restartReliabilityStack();
    await waitForStack(request);
    await assertApiGraph(request, buildCid, FIRST_MARKER, previewBearers);
    await assertApiGraph(request, agentCid, FIRST_MARKER, previewBearers);
    await page.reload();
    await openFromHandoff(context, page, buildCid, FIRST_MARKER, previewBearers);
    await agentPage.reload();
    await openFromHandoff(context, agentPage, agentCid, FIRST_MARKER, previewBearers);

    const beforeRevision = Math.max(0, ...buildFirst.events.map((event) => event.seq ?? 0));
    const revise = await authenticatedMutation(
      request,
      `${AGENT_API}/conversations/${buildCid}/messages`,
      {
        data: {
          content:
            `Revise only the selected release. Change the visible h1 in ${RELEASE_ENTRY} ` +
            `from '${FIRST_MARKER}' to exactly '${SECOND_MARKER}'. Keep every external asset ` +
            `and route intact, preserve the exact '<title>${RELEASE_TITLE}</title>', serve ` +
            `exactly '${RELEASE_ENTRY}', verify, and finish. Never serve ` +
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
    await assertApiGraph(request, buildCid, SECOND_MARKER, previewBearers, undefined, FIRST_MARKER);
    await assertApiGraph(
      request,
      buildCid,
      FIRST_MARKER,
      previewBearers,
      buildFirst.version,
      SECOND_MARKER,
    );
    await openFromHandoff(context, page, buildCid, SECOND_MARKER, previewBearers);

    const safeConsoleErrors = consoleErrors.map((message) =>
      redactPreviewBearers(message, previewBearers),
    );
    const safeFailedPreviewRequests = failedPreviewRequests.map((message) =>
      redactPreviewBearers(message, previewBearers),
    );
    expect(safeConsoleErrors, `browser console errors: ${JSON.stringify(safeConsoleErrors)}`).toEqual([]);
    expect(
      safeFailedPreviewRequests,
      `failed preview requests: ${JSON.stringify(safeFailedPreviewRequests)}`,
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
      console_errors: safeConsoleErrors,
      failed_preview_requests: safeFailedPreviewRequests,
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
