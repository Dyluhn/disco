import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import {
  addMcpThroughSettings,
  approveMcpUntilConnected,
  freeLoopbackPort,
  removeMcpThroughSettings,
  startMcpFixture,
  stopMcpFixture,
  type McpFixture,
} from "./mcp-reliability-helpers";
import {
  AGENT_API,
  authenticatedMutation,
  getJson,
  requireReliabilityStack,
  restartReliabilityStack,
} from "./reliability-helpers";

type RuntimeServer = {
  name: string;
  enabled: boolean;
  status: string;
  approval_required?: boolean;
  diagnostic?: {
    code?: string;
    attempts?: number;
    exception_type?: string;
    [key: string]: unknown;
  };
};

type RuntimeMcp = {
  enabled: boolean;
  servers: Record<string, RuntimeServer>;
};

type AuthoringContext = {
  mcp_servers: Array<{ server: string; tools: string[] }>;
};

function rowFor(page: Page, name: string) {
  return page.locator("li").filter({ hasText: name });
}

async function runtimeMcp(request: APIRequestContext): Promise<RuntimeMcp> {
  return getJson<RuntimeMcp>(request, `${AGENT_API}/api/mcp/servers`);
}

async function authoringContext(request: APIRequestContext): Promise<AuthoringContext> {
  return getJson<AuthoringContext>(
    request,
    `${AGENT_API}/api/workflows/authoring-context`,
  );
}

async function waitForRuntimeStatus(
  request: APIRequestContext,
  name: string,
  status: string,
): Promise<RuntimeServer> {
  await expect
    .poll(
      async () => (await runtimeMcp(request)).servers[name]?.status ?? "missing",
      { timeout: 90_000, intervals: [250, 500, 1_000] },
    )
    .toBe(status);
  return (await runtimeMcp(request)).servers[name];
}

async function expectMounted(
  request: APIRequestContext,
  name: string,
  mounted: boolean,
): Promise<void> {
  await expect
    .poll(
      async () => {
        const context = await authoringContext(request);
        return context.mcp_servers.some((server) => server.server === name);
      },
      { timeout: 60_000, intervals: [250, 500, 1_000] },
    )
    .toBe(mounted);
}

async function approveNext(
  page: Page,
  name: string,
  expectedStatus = 200,
): Promise<string> {
  const row = rowFor(page, name);
  const review = row.locator('[data-disco-control="settings.mcp-reapprove"]');
  await expect(review).toBeVisible({ timeout: 60_000 });
  await review.click();
  const confirm = row.locator(
    '[data-disco-control="settings.mcp-reapprove-confirm"]',
  );
  await expect(confirm).toBeVisible({ timeout: 30_000 });
  const responsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname.endsWith(
        `/api/mcp/servers/${encodeURIComponent(name)}/approve`,
      ),
    { timeout: 60_000 },
  );
  await confirm.click();
  const response = await responsePromise;
  const body = await response.text();
  expect(response.status(), body).toBe(expectedStatus);
  return body;
}

async function toggleServer(page: Page, name: string, enabled: boolean): Promise<void> {
  const row = rowFor(page, name);
  const toggle = row.getByRole("switch", { name: `Enable ${name}` });
  await expect(toggle).toHaveAttribute("aria-checked", String(!enabled), {
    timeout: 60_000,
  });
  const responsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "PATCH" &&
      new URL(response.url()).pathname.endsWith(
        `/api/mcp/servers/${encodeURIComponent(name)}`,
      ),
    { timeout: 60_000 },
  );
  await toggle.click();
  const response = await responsePromise;
  expect(response.ok(), await response.text()).toBe(true);
  await expect(toggle).toHaveAttribute("aria-checked", String(enabled), {
    timeout: 60_000,
  });
}

test("mixed MCP servers degrade, recover, revoke, and survive restart without hidden fallback", async ({
  page,
  request,
}, testInfo) => {
  test.setTimeout(600_000);
  expect(
    /^(1|true|yes)$/i.test(process.env.DISCO_RELIABILITY_ISOLATED_STACK ?? ""),
    "this suite mutates MCP configuration and must target an isolated reliability stack",
  ).toBe(true);
  await requireReliabilityStack(request);
  expect(await runtimeMcp(request)).toEqual({ enabled: false, servers: {} });

  const suffix = `${process.pid}_${testInfo.workerIndex}_${Date.now()}`;
  const reachableName = `reliability_reachable_${suffix}`;
  const recoveringName = `reliability_recovering_${suffix}`;
  const invalidName = `reliability_invalid_${suffix}`;
  const recoveryPort = await freeLoopbackPort();
  const recoveryUrl = `http://127.0.0.1:${recoveryPort}/mcp`;
  const reachable = await startMcpFixture(testInfo, {
    label: "reachable",
    allowExternal: false,
  });
  let recovering: McpFixture | null = null;

  try {
    await page.goto("/settings");
    await expect(page.getByRole("heading", { name: "Settings", exact: true })).toBeVisible();

    // H021/SEC008: malformed credential-bearing URLs fail transactionally and
    // the rejected input is not reflected across the API boundary.
    await page.locator('[data-disco-control="settings.mcp-add"]').click();
    await page.getByLabel("MCP server name").fill(invalidName);
    const rejectedUrl = "https://operator:credential@mcp.invalid/tools";
    await page.getByLabel("MCP server URL").fill(rejectedUrl);
    const rejectedPromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        new URL(response.url()).pathname.endsWith("/api/mcp/servers"),
      { timeout: 60_000 },
    );
    await page.locator('[data-disco-control="settings.mcp-add-save"]').click();
    const rejected = await rejectedPromise;
    const rejectedBody = await rejected.text();
    expect(rejected.status(), rejectedBody).toBe(400);
    expect(rejectedBody).toContain("absolute HTTP(S) URL");
    expect(rejectedBody).not.toContain("credential");
    await expect(page.getByRole("alert").filter({ hasText: "absolute HTTP(S) URL" })).toBeVisible();
    expect((await runtimeMcp(request)).servers).not.toHaveProperty(invalidName);
    await page.getByRole("button", { name: "Cancel", exact: true }).click();

    await addMcpThroughSettings(page, {
      name: reachableName,
      url: reachable.mcpUrl,
    });
    await approveMcpUntilConnected(page, reachableName);
    await waitForRuntimeStatus(request, reachableName, "connected");
    await expectMounted(request, reachableName, true);

    await addMcpThroughSettings(page, {
      name: recoveringName,
      url: recoveryUrl,
    });
    await approveNext(page, recoveringName);
    const degraded = await waitForRuntimeStatus(request, recoveringName, "degraded");
    expect(degraded.diagnostic).toEqual({
      code: "mcp_connection_failed",
      attempts: 3,
      exception_type: "ConnectError",
    });
    expect(JSON.stringify(degraded)).not.toContain("127.0.0.1");
    await expectMounted(request, reachableName, true);
    await expectMounted(request, recoveringName, false);

    // F10/F14: a failed optional sibling neither kills readiness nor lies about
    // the reachable server, including after a full App+Agent restart.
    // Quiesce the browser first: an active shell polls /api/activity and would
    // make Vite report a harness-caused ECONNREFUSED during the intentional stop.
    await page.goto("about:blank");
    await restartReliabilityStack();
    await waitForRuntimeStatus(request, reachableName, "connected");
    const restartedDegraded = await waitForRuntimeStatus(
      request,
      recoveringName,
      "degraded",
    );
    expect(restartedDegraded.diagnostic?.attempts).toBe(3);
    await expectMounted(request, reachableName, true);
    await page.goto("/settings");
    await expect(rowFor(page, reachableName).locator('[data-mcp-status="connected"]')).toBeVisible({
      timeout: 60_000,
    });
    await expect(rowFor(page, recoveringName).locator('[data-mcp-status="degraded"]')).toBeVisible({
      timeout: 60_000,
    });

    // Recovery is explicit and bounded: the dead endpoint comes online, one
    // hot reload discovers it, and tool approval mounts it without a restart.
    recovering = await startMcpFixture(testInfo, {
      port: recoveryPort,
      label: "recovering",
      allowExternal: false,
    });
    const reload = await authenticatedMutation(request, `${AGENT_API}/api/mcp/reload`, {
      timeout: 90_000,
    });
    expect(reload.ok(), await reload.text()).toBe(true);
    await waitForRuntimeStatus(request, recoveringName, "approval_required");
    await page.reload();
    await approveNext(page, recoveringName);
    await waitForRuntimeStatus(request, recoveringName, "connected");
    await expectMounted(request, recoveringName, true);

    // SEC006/F11: revocation hot-unmounts tools without deleting configuration.
    const reachableRow = rowFor(page, reachableName);
    const revokePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        new URL(response.url()).pathname.endsWith(
          `/api/mcp/servers/${encodeURIComponent(reachableName)}/revoke`,
        ),
      { timeout: 60_000 },
    );
    await reachableRow.locator('[data-disco-control="settings.mcp-revoke"]').click();
    const revoked = await revokePromise;
    expect(revoked.ok(), await revoked.text()).toBe(true);
    await waitForRuntimeStatus(request, reachableName, "approval_required");
    await expectMounted(request, reachableName, false);
    await expect(rowFor(page, reachableName)).toBeVisible();

    // F08/H023: disabled pending approvals are blocked with the real actionable
    // reason, while status/config/runtime all agree on Disabled.
    await toggleServer(page, reachableName, false);
    await waitForRuntimeStatus(request, reachableName, "disabled");
    await expect(rowFor(page, reachableName).locator('[data-mcp-status="disabled"]')).toBeVisible({
      timeout: 60_000,
    });
    const blockedBody = await approveNext(page, reachableName, 409);
    expect(blockedBody).toContain("enable it before approving");
    await expect(page.getByRole("alert").filter({ hasText: "enable it before approving" })).toBeVisible();
    await rowFor(page, reachableName).getByRole("button", { name: "Dismiss" }).click();

    await toggleServer(page, reachableName, true);
    await approveMcpUntilConnected(page, reachableName);
    await waitForRuntimeStatus(request, reachableName, "connected");
    await expectMounted(request, reachableName, true);

    await page.goto("about:blank");
    await restartReliabilityStack();
    await waitForRuntimeStatus(request, reachableName, "connected");
    await waitForRuntimeStatus(request, recoveringName, "connected");
    await expectMounted(request, reachableName, true);
    await expectMounted(request, recoveringName, true);

    await page.goto("/settings");
    await removeMcpThroughSettings(page, recoveringName);
    await removeMcpThroughSettings(page, reachableName);
    await expect
      .poll(async () => await runtimeMcp(request), {
        timeout: 60_000,
        intervals: [250, 500, 1_000],
      })
      .toEqual({ enabled: false, servers: {} });
  } finally {
    await removeMcpThroughSettings(page, recoveringName).catch(() => undefined);
    await removeMcpThroughSettings(page, reachableName).catch(() => undefined);
    if (recovering) await stopMcpFixture(recovering);
    await stopMcpFixture(reachable);
  }
});
