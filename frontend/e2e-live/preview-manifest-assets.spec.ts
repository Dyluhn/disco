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
import {
  attemptAllCleanup,
  createRegisteredTarget,
  rethrowAfterBestEffortReport,
} from "@/lib/harness/cleanupOracle";
import {
  expectedBrowserConsoleError,
  type ExpectedBrowserConsoleError,
} from "@/lib/harness/browserConsoleOracle";
import {
  redactFailureStringsInPlace,
  redactKnownIdentifiers,
} from "@/lib/harness/evidenceRedaction";
import { updateProviderConversationManifest } from "@/lib/harness/providerManifest";
import {
  ISOLATED_PREVIEW_PREFIX,
  isIsolatedPreviewUrl,
  isStaticPreviewHostnameForCid,
} from "@/lib/harness/previewUrlOracle";
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
   './development/scripts/app.js?mode=live', an img id hero src
   './media/hero%20image.svg?asset=1#hero', and an anchor id nested-link href
   './development/notes/?view=full#nested'. Do not inline any CSS, JavaScript, image, or font.
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

async function createConversation(
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
  return String((await create.json()).conversation_id);
}

async function kickRun(
  request: APIRequestContext,
  surface: Surface,
  cid: string,
): Promise<void> {
  const kick = await authenticatedMutation(
    request,
    `${AGENT_API}/conversations/${cid}/messages`,
    { data: { content: buildPrompt(surface) }, timeout: 30_000 },
  );
  expect(kick.ok(), await kick.text()).toBe(true);
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
  await expect(approve).toBeHidden({ timeout: 30_000 });
}

async function waitForCommittedFinish(
  request: APIRequestContext,
  cid: string,
  page: Page,
  afterSeq = 0,
): Promise<{ version: number; events: EventJson[]; additionalPlanApprovals: number }> {
  const deadline = Date.now() + 900_000;
  let latest: EventJson[] = [];
  const approvedPendingPlanIds = new Set<string>();
  while (Date.now() < deadline) {
    latest = await allEvents(request, cid);
    const terminal = latestStoppedStatus(latest, afterSeq);
    if (terminal) {
      throw new Error(`conversation reached ${terminal.status}: ${String(terminal.detail ?? "")}`);
    }
    const latestStatus = [...latest]
      .reverse()
      .find((event) => event.kind === "status" && (event.seq ?? 0) > afterSeq);
    if (latestStatus?.status === "AWAITING_PLAN_APPROVAL") {
      const pendingPlanId = String(latestStatus.detail ?? "");
      expect(pendingPlanId, "plan-approval status omitted its pending plan id").not.toBe("");
      expect(
        latest.some((event) => event.kind === "plan" && event.id === pendingPlanId),
        `pending plan ${pendingPlanId} has no durable PlanEvent`,
      ).toBe(true);
      if (!approvedPendingPlanIds.has(pendingPlanId)) {
        approvedPendingPlanIds.add(pendingPlanId);
        await approveBuildPlan(page);
        continue;
      }
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
        return {
          version,
          events: latest,
          additionalPlanApprovals: approvedPendingPlanIds.size,
        };
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
  throw new Error(`conversation did not finish with a durable version after seq ${afterSeq}`);
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
): Promise<void> {
  const targetPath = version === undefined ? "/" : `/?version=${version}`;
  const mint = await authenticatedMutation(
    request,
    `${AGENT_API}/conversations/${cid}/preview/capability`,
    {
      data: { target_path: targetPath, transport: "path" },
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

async function assertRenderedDocument(
  page: Page,
  marker: string,
  absentVisibleMarker?: string,
): Promise<void> {
  const body = page.locator("body");
  await expect(body).toContainText(marker, { timeout: 120_000 });
  await expect(page.locator("h1")).toHaveText(marker);
  if (absentVisibleMarker) await expect(body).not.toContainText(absentVisibleMarker);
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
  absentVisibleMarker?: string,
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
      await assertRenderedDocument(popup, marker, absentVisibleMarker);
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
        target_path: "/",
        transport: "path",
      });
      expect(capabilityResponse.headers()["cache-control"]).toContain("no-store");
      const capability = (await capabilityResponse.json()) as {
        bootstrap_url: string;
        bootstrap_intent: string;
        port: number;
      };
      expect([3000, 4321, 5000, 5173, 8000, 8080]).toContain(capability.port);
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
          new RegExp(`^p3s-${compactCid}-[0-9a-f]{40}-${capability.port}\\.`),
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
        const workerProbe = await popup.evaluate(async () => {
          if (!("serviceWorker" in navigator)) {
            return {
              supported: false,
              registrationSucceeded: false,
              rejection: null,
              registrationsAfterAttempt: -1,
              registrationsAfterCleanup: -1,
            };
          }
          let registrationSucceeded = false;
          let rejection: string | null = null;
          try {
            await navigator.serviceWorker.register("/sw.js");
            registrationSucceeded = true;
          } catch (error) {
            rejection = error instanceof Error ? error.name : typeof error;
          }
          const registrations = await navigator.serviceWorker.getRegistrations();
          const registrationsAfterAttempt = registrations.length;
          // A policy regression must fail below, but must not contaminate later
          // assertions or runs with a service worker that unexpectedly registered.
          await Promise.all(registrations.map((registration) => registration.unregister()));
          return {
            supported: true,
            registrationSucceeded,
            rejection,
            registrationsAfterAttempt,
            registrationsAfterCleanup: (await navigator.serviceWorker.getRegistrations()).length,
          };
        });
        expect(workerProbe.supported, "Firefox did not expose serviceWorker on p3s").toBe(true);
        expect(
          workerProbe.registrationSucceeded,
          "p3s accepted a generated service worker",
        ).toBe(false);
        expect(
          typeof workerProbe.rejection === "string" && workerProbe.rejection.length > 0,
          "Firefox service-worker registration did not reject with an error",
        ).toBe(true);
        expect(
          workerProbe.registrationsAfterAttempt,
          "p3s retained a service-worker registration after the rejected attempt",
        ).toBe(0);
        expect(
          workerProbe.registrationsAfterCleanup,
          "service-worker probe cleanup left a registration behind",
        ).toBe(0);

        // Firefox does not reliably expose a failed service-worker script fetch
        // through BrowserContext's response event. Independently exercise the
        // exact policy route with the popup's HttpOnly capability cookie. The
        // cookie is path-scoped, so an explicit header is required for /sw.js;
        // keep it only in memory in a fresh, untraced API context.
        const capabilityCookieName = `disco_path_preview_${compactCid}`;
        const capabilityCookies = (await context.cookies([popup.url()])).filter(
          (cookie) => cookie.name === capabilityCookieName,
        );
        expect(
          capabilityCookies.length,
          "popup did not retain exactly one scoped preview capability cookie",
        ).toBe(1);
        const capabilityCookie = capabilityCookies[0];
        expect(capabilityCookie.httpOnly, "preview capability cookie was not HttpOnly").toBe(true);
        expect(capabilityCookie.domain, "preview capability cookie escaped its p3s host").toBe(
          finalUrl.hostname,
        );
        expect(
          capabilityCookie.path,
          "preview capability cookie path was not conversation-scoped",
        ).toBe(`${ISOLATED_PREVIEW_PREFIX}/${cid}/`);
        expect(
          capabilityCookie.value.length,
          "preview capability cookie was empty",
        ).toBeGreaterThan(0);

        const workerUrl = new URL("/sw.js", finalUrl.origin).toString();
        const workerRequest = await playwrightRequest.newContext({
          extraHTTPHeaders: {
            Cookie: `${capabilityCookie.name}=${capabilityCookie.value}`,
          },
        });
        try {
          const workerResponse = await workerRequest.get(workerUrl, {
            headers: { "Service-Worker": "script" },
            maxRedirects: 0,
            timeout: 30_000,
          });
          expect(workerResponse.url(), "service-worker policy probe changed URL").toBe(workerUrl);
          expect(
            workerResponse.status(),
            "p3s service-worker policy did not return 403",
          ).toBe(403);
          expect(workerResponse.headers()["cache-control"]).toBe("private, no-store");
          expect(await workerResponse.text()).toBe("preview service workers disabled");
        } finally {
          await workerRequest.dispose();
        }
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
    expect(isStaticPreviewHostnameForCid(new URL(first.url()).hostname, firstCid)).toBe(true);
    expect(isStaticPreviewHostnameForCid(new URL(second.url()).hostname, secondCid)).toBe(true);
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
    // Completed static builds use the same canonical Preview contract as an
    // active runtime; the immutable committed authority is hidden behind it.
    const iframe = page.locator('iframe[title="Preview"]').first();
    await expect(iframe).toBeVisible({ timeout: 120_000 });
    const attachedFrame = await (await iframe.elementHandle())?.contentFrame();
    expect(attachedFrame, "canonical Preview iframe did not attach").not.toBeNull();
    const frameUrl = new URL(attachedFrame!.url());
    expect(frameUrl.hostname).toMatch(/^127(?:\.\d+){3}$/);
    expect(frameUrl.origin).not.toBe(new URL(page.url()).origin);
    const frame = iframe.contentFrame();
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
  const expectedConsoleErrors: Record<ExpectedBrowserConsoleError, number> = {
    "firefox-internal-favicon-csp": 0,
    "planned-agent-restart": 0,
  };
  const failedPreviewRequests: string[] = [];
  const isolatedPreviewHttpFailures: string[] = [];
  const previewBearers: string[] = [];
  let plannedAgentRestart = false;
  const observe = (candidate: Page) => {
    candidate.on("console", (message) => {
      if (message.type() !== "error") return;
      const expected = expectedBrowserConsoleError(message.text(), plannedAgentRestart);
      if (expected) {
        expectedConsoleErrors[expected] += 1;
      } else {
        consoleErrors.push(message.text());
      }
    });
    candidate.on("requestfailed", (failed) => {
      if (isIsolatedPreviewUrl(failed.url())) {
        failedPreviewRequests.push(`${failed.url()}: ${failed.failure()?.errorText ?? "unknown"}`);
      }
    });
    candidate.on("response", (response) => {
      if (isIsolatedPreviewUrl(response.url()) && response.status() >= 500) {
        isolatedPreviewHttpFailures.push(`${response.status()} ${response.url()}`);
      }
    });
  };
  observe(page);
  context.on("page", observe);

  const cids: string[] = [];
  let evidence: Record<string, unknown> | undefined;
  let primaryFailed = false;
  let primaryError: unknown;
  try {
    const buildCid = await createRegisteredTarget(
      () => createConversation(request, "build"),
      (cid) => {
        cids.push(cid);
        updateProviderConversationManifest(cids);
      },
      (cid) => kickRun(request, "build", cid),
    );
    await page.goto(`/build/${buildCid}`);
    await approveBuildPlan(page);
    const buildFirst = await waitForCommittedFinish(request, buildCid, page);
    const buildFirstVerifier = verifierEvidence(buildFirst.events);
    expect(selectedApp(buildFirst.events)?.path).toBe(RELEASE_ENTRY);
    assertNoThrash(buildFirst.events, await inspectTrace(request, buildCid));
    await assertApiGraph(request, buildCid, FIRST_MARKER, previewBearers);
    await openFromHandoff(context, page, buildCid, FIRST_MARKER, previewBearers);
    const iframeResponses = await assertBuildIframe(page, buildCid, FIRST_MARKER);

    const agentPage = await context.newPage();
    const agentCid = await createRegisteredTarget(
      () => createConversation(request, "agent"),
      (cid) => {
        cids.push(cid);
        updateProviderConversationManifest(cids);
      },
      (cid) => kickRun(request, "agent", cid),
    );
    await agentPage.goto(`/agent/${agentCid}`);
    const agentFirst = await waitForCommittedFinish(request, agentCid, agentPage);
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

    plannedAgentRestart = true;
    try {
      await restartReliabilityStack();
      await waitForStack(request);
      await assertApiGraph(request, buildCid, FIRST_MARKER, previewBearers);
      await assertApiGraph(request, agentCid, FIRST_MARKER, previewBearers);
      await page.reload();
      await openFromHandoff(context, page, buildCid, FIRST_MARKER, previewBearers);
      await agentPage.reload();
      await openFromHandoff(context, agentPage, agentCid, FIRST_MARKER, previewBearers);
    } finally {
      plannedAgentRestart = false;
    }

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
    const buildSecond = await waitForCommittedFinish(request, buildCid, page, beforeRevision);
    const buildSecondVerifier = verifierEvidence(buildSecond.events);
    expect(selectedApp(buildSecond.events)?.path).toBe(RELEASE_ENTRY);
    expect(buildSecond.version).toBeGreaterThan(buildFirst.version);
    assertNoThrash(buildSecond.events, await inspectTrace(request, buildCid));
    await assertApiGraph(request, buildCid, SECOND_MARKER, previewBearers);
    await assertApiGraph(request, buildCid, FIRST_MARKER, previewBearers, buildFirst.version);
    await openFromHandoff(
      context,
      page,
      buildCid,
      SECOND_MARKER,
      previewBearers,
      FIRST_MARKER,
    );

    const sanitizeEvidence = (message: string) =>
      redactKnownIdentifiers(redactPreviewBearers(message, previewBearers), cids);
    const safeConsoleErrors = consoleErrors.map(sanitizeEvidence);
    const safeFailedPreviewRequests = failedPreviewRequests.map(sanitizeEvidence);
    const safeIsolatedPreviewHttpFailures = isolatedPreviewHttpFailures.map(sanitizeEvidence);
    expect(safeConsoleErrors, `browser console errors: ${JSON.stringify(safeConsoleErrors)}`).toEqual([]);
    expect(
      safeFailedPreviewRequests,
      `failed preview requests: ${JSON.stringify(safeFailedPreviewRequests)}`,
    ).toEqual([]);
    expect(
      safeIsolatedPreviewHttpFailures,
      `isolated preview HTTP 5xx responses: ${JSON.stringify(safeIsolatedPreviewHttpFailures)}`,
    ).toEqual([]);

    evidence = {
      revision: process.env.DISCO_RELIABILITY_REVISION ?? null,
      expected_console_errors: expectedConsoleErrors,
      build: {
        first_version: buildFirst.version,
        second_version: buildSecond.version,
        additional_plan_approvals:
          buildFirst.additionalPlanApprovals + buildSecond.additionalPlanApprovals,
        selected_entry: selectedApp(buildSecond.events)?.path,
        event_count: buildSecond.events.length,
        first_verifier: buildFirstVerifier,
        second_verifier: buildSecondVerifier,
      },
      agent: {
        additional_plan_approvals: agentFirst.additionalPlanApprovals,
        version: agentFirst.version,
        selected_entry: selectedApp(agentFirst.events)?.path,
        event_count: agentFirst.events.length,
        verifier: agentFirstVerifier,
      },
      iframe_responses: iframeResponses.map(sanitizeEvidence),
      console_errors: safeConsoleErrors,
      failed_preview_requests: safeFailedPreviewRequests,
      isolated_preview_http_failures: safeIsolatedPreviewHttpFailures,
    };
  } catch (error) {
    primaryFailed = true;
    primaryError = redactFailureStringsInPlace(error, (value) =>
      redactKnownIdentifiers(redactPreviewBearers(value, previewBearers), cids),
    );
  } finally {
    context.removeAllListeners("page");
  }
  const cleanup = await attemptAllCleanup(cids, (cid) => deleteConversation(request, cid));
  if (cleanup.failed > 0) {
    if (primaryFailed) {
      testInfo.annotations.push({
        type: "conversation-cleanup-failure",
        description: `${cleanup.failed} of ${cleanup.attempted} cleanup targets failed`,
      });
      await rethrowAfterBestEffortReport(primaryError, () =>
        testInfo.attach("conversation-cleanup-failure.json", {
          body: Buffer.from(JSON.stringify(cleanup), "utf-8"),
          contentType: "application/json",
        }),
      );
    } else {
      throw new Error(
        `conversation cleanup failed for ${cleanup.failed} of ${cleanup.attempted} targets`,
      );
    }
  }
  if (primaryFailed) throw primaryError;
  if (evidence === undefined) throw new Error("preview success evidence was not assembled");
  fs.writeFileSync(
    testInfo.outputPath("preview-manifest-assets-evidence.json"),
    JSON.stringify({ ...evidence, cleanup }, null, 2),
    "utf-8",
  );
});
