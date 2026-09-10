import { expect, type Page, type TestInfo } from "@playwright/test";
import { spawn, type ChildProcess } from "node:child_process";
import * as fs from "node:fs";
import { createServer } from "node:net";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

// This file lives at `current/frontend/e2e-live/`, so the repo root is THREE
// levels up; `../..` landed on `current/`, whose `.venv/bin/python3` does not
// exist, and the spawn then failed with an ENOENT carrying no status and no
// output. `harness` is not an installed distribution either — pytest reaches it
// through `pythonpath = [".", "development", "current"]` in pyproject.toml,
// which is pytest-only, so a subprocess needs `development` on PYTHONPATH.
const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const FIXTURE_PYTHONPATH = [path.join(REPO, "development"), process.env.PYTHONPATH]
  .filter(Boolean)
  .join(path.delimiter);

export type McpFixture = {
  process: ChildProcess | null;
  mcpUrl: string;
  callsUrl: string | null;
};

export type McpDraft = {
  name: string;
  url: string;
  transport?: "streamable_http" | "stdio";
  riskTier?: "low" | "medium" | "high" | "critical";
};

export async function startMcpFixture(
  testInfo: TestInfo,
  options: { port?: number; label?: string; allowExternal?: boolean } = {},
): Promise<McpFixture> {
  const external =
    options.allowExternal === false
      ? ""
      : process.env.DISCO_RELIABILITY_MCP_URL?.trim();
  if (external) return { process: null, mcpUrl: external, callsUrl: null };

  const label = options.label ? `-${options.label.replace(/[^a-z0-9_-]/gi, "_")}` : "";
  const ready = testInfo.outputPath(`mcp-fixture${label}-ready.json`);
  const calls = testInfo.outputPath(`mcp-fixture${label}-calls.jsonl`);
  fs.mkdirSync(path.dirname(ready), { recursive: true });
  const python =
    process.env.DISCO_RELIABILITY_PYTHON ?? path.join(REPO, ".venv", "bin", "python3");
  const child = spawn(
    python,
    [
      "-m",
      "harness.reliability.mcp_fixture",
      "--port",
      String(options.port ?? 0),
      "--ready-file",
      ready,
      "--log",
      calls,
    ],
    {
      cwd: REPO,
      stdio: ["ignore", "ignore", "pipe"],
      env: { ...process.env, PYTHONPATH: FIXTURE_PYTHONPATH },
    },
  );
  let stderr = "";
  child.stderr?.on("data", (chunk) => {
    stderr = (stderr + String(chunk)).slice(-8_000);
  });

  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline && !fs.existsSync(ready)) {
    if (child.exitCode !== null) {
      throw new Error(`reliability MCP fixture exited ${child.exitCode}: ${stderr}`);
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  if (!fs.existsSync(ready)) {
    child.kill("SIGTERM");
    throw new Error(`reliability MCP fixture did not become ready: ${stderr}`);
  }
  const payload = JSON.parse(fs.readFileSync(ready, "utf-8")) as { mcp_url: string };
  const base = payload.mcp_url.replace(/\/mcp$/, "");
  return { process: child, mcpUrl: payload.mcp_url, callsUrl: `${base}/calls` };
}

export async function freeLoopbackPort(): Promise<number> {
  const server = createServer();
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  if (!address || typeof address === "string") {
    server.close();
    throw new Error("could not reserve a loopback port for MCP recovery");
  }
  const port = address.port;
  await new Promise<void>((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
  return port;
}

export async function stopMcpFixture(fixture: McpFixture): Promise<void> {
  const child = fixture.process;
  if (!child || child.exitCode !== null) return;
  child.kill("SIGTERM");
  await Promise.race([
    new Promise<void>((resolve) => child.once("exit", () => resolve())),
    new Promise<void>((resolve) => setTimeout(resolve, 5_000)),
  ]);
  if (child.exitCode === null) child.kill("SIGKILL");
}

/**
 * Exercise the real Settings mutation boundary and prove that the browser sent
 * the persisted POST. Waiting on the response is intentional: a rendered row
 * alone could come from fixture/optimistic state and a coordinate click can be
 * lost if an unfinished Settings query shifts the long page underneath it.
 */
export async function addMcpThroughSettings(page: Page, draft: McpDraft): Promise<void> {
  const add = page.locator('[data-disco-control="settings.mcp-add"]');
  await expect(add).toBeEnabled({ timeout: 60_000 });
  await add.click();
  await page.getByLabel("MCP server name").fill(draft.name);
  await page.getByLabel("MCP server URL").fill(draft.url);
  await page
    .getByLabel("Transport type")
    .selectOption(draft.transport ?? "streamable_http");
  await page.getByLabel("Risk tier").selectOption(draft.riskTier ?? "low");

  const responsePromise = page.waitForResponse(
    (response) => {
      const request = response.request();
      return (
        request.method() === "POST" &&
        new URL(response.url()).pathname.endsWith("/api/mcp/servers")
      );
    },
    { timeout: 60_000 },
  );
  await page.locator('[data-disco-control="settings.mcp-add-save"]').click();
  const response = await responsePromise;
  const body = await response.text();
  expect(
    response.ok(),
    `Settings MCP save failed: ${response.status()} ${body}`,
  ).toBe(true);
  expect(response.request().postDataJSON()).toMatchObject({
    name: draft.name,
    url: draft.url,
    transport: draft.transport ?? "streamable_http",
    risk_tier: draft.riskTier ?? "low",
    enabled: true,
  });
}

export async function approveMcpUntilConnected(page: Page, name: string): Promise<void> {
  const row = page.locator("li").filter({ hasText: name });
  await expect(row).toBeVisible({ timeout: 60_000 });
  for (let approval = 0; approval < 3; approval += 1) {
    const review = row.locator('[data-disco-control="settings.mcp-reapprove"]');
    if ((await review.count()) === 0) break;
    await review.click();
    const confirm = row.locator('[data-disco-control="settings.mcp-reapprove-confirm"]');
    await expect(confirm).toBeVisible({ timeout: 30_000 });
    await confirm.click();
    await expect(confirm).toBeHidden({ timeout: 60_000 });
    await expect.poll(async () => review.count(), { timeout: 60_000 }).toBeLessThanOrEqual(1);
  }
  await expect(
    row.locator('[data-mcp-status="connected"]'),
    "MCP did not become connected after config and tool-schema approvals",
  ).toBeVisible({ timeout: 60_000 });
  await expect(row.locator('[data-disco-control="settings.mcp-reapprove"]')).toHaveCount(0);

  await row.locator('[data-disco-control="settings.mcp-test"]').click();
  await expect(row.locator('[data-probe-ok="true"]')).toBeVisible({ timeout: 60_000 });
}

export async function removeMcpThroughSettings(page: Page, name: string): Promise<void> {
  await page.goto("/settings").catch(() => undefined);
  // Wait for the authoritative MCP query to settle before deciding the row is
  // absent. Checking immediately after navigation can see the empty loading
  // render, skip cleanup, and falsely claim runtime removal was exercised.
  await expect(page.locator('[data-disco-control="settings.mcp-add"]')).toBeEnabled({
    timeout: 60_000,
  });
  const row = page.locator("li").filter({ hasText: name });
  if ((await row.count().catch(() => 0)) === 0) return;
  await row.locator('[data-disco-control="settings.mcp-remove"]').click();
  await expect(row).toHaveCount(0, { timeout: 60_000 });
}
