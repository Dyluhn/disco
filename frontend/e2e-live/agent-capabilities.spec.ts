import { expect, test } from "@playwright/test";
import * as fs from "node:fs";

import {
  AGENT_API,
  allEvents,
  assertNoThrash,
  deleteConversation,
  getJson,
  inspectTrace,
  requireReliabilityStack,
  waitForStatus,
} from "./reliability-helpers";
import {
  addMcpThroughSettings,
  approveMcpUntilConnected,
  removeMcpThroughSettings,
  startMcpFixture,
  stopMcpFixture,
} from "./mcp-reliability-helpers";

const TOOL = "reliability_echo";
const TOKEN = "DISCO_MCP_LIVE_PROOF";
const RESULT = "MCP_RUNTIME_CALL_SUCCEEDED";
const OUTPUT = "outputs/reliability-mcp.md";

type WorkflowReview = {
  instance_id: string;
  name: string;
  approved: boolean;
  validation_findings: Array<{ severity?: string; code?: string }>;
  compiled_surface: {
    compiled: boolean;
    allowed_tools: string[];
    advertised_tools: string[];
    mcp_mounts: Array<Record<string, unknown>>;
  };
};

test("approved MCP hot-reloads, seals into a workflow, executes, and produces output", async ({
  page,
  request,
}, testInfo) => {
  test.setTimeout(1_200_000);
  expect(
    /^(1|true|yes)$/i.test(process.env.DISCO_RELIABILITY_ISOLATED_STACK ?? ""),
    "this suite mutates MCP configuration and must target an isolated reliability stack",
  ).toBe(true);
  await requireReliabilityStack(request);

  const fixture = await startMcpFixture(testInfo);
  const suffix = `${process.pid}_${testInfo.workerIndex}_${testInfo.repeatEachIndex}_${Date.now()}`;
  const serverName = `reliability_${suffix}`;
  const workflowName = `Reliability MCP ${suffix}`;
  const qualified = `mcp__${serverName}__${TOOL}`;
  let cid = "";

  try {
    // Configure through the real Settings UI. Each save/approval must hot-reload
    // the running Agent server; the authoring-context assertion below detects a
    // persisted-but-not-applied configuration.
    await page.goto("/settings");
    await addMcpThroughSettings(page, { name: serverName, url: fixture.mcpUrl });
    await approveMcpUntilConnected(page, serverName);

    const context = await getJson<{
      mcp_servers: Array<{ server: string; tools: string[] }>;
    }>(request, `${AGENT_API}/api/workflows/authoring-context`);
    const mountedServer = context.mcp_servers.find((item) => item.server === serverName);
    expect(mountedServer, "running Agent authoring context did not hot-reload the MCP server").toBeTruthy();
    expect(mountedServer?.tools).toContain(TOOL);

    // Author the bounded workflow through the product UI. This checks the
    // current tool inventory, review/simulation, sealed surface, and approval.
    await page.goto("/workflows");
    await page.getByRole("button", { name: "Create workflow" }).click();
    await page.getByRole("button", { name: "Edit details (Advanced)" }).click();
    await page.getByLabel("Name", { exact: true }).fill(workflowName);
    await page.getByLabel("Card", { exact: true }).fill(
      `Call ${qualified} exactly once with token ${TOKEN}. Then write the complete ` +
        `tool result, including ${RESULT}, to ${OUTPUT} and finish only after the file exists.`,
    );

    const fileWriteLabel = page.locator("label").filter({
      has: page.getByText("file_write", { exact: true }),
    });
    await fileWriteLabel.getByRole("checkbox").check();
    await page.getByText("MCP mounts", { exact: true }).click();
    await page.getByRole("button", { name: "Add MCP mount" }).click();
    await page.getByLabel("MCP mount 1 server").selectOption(serverName);
    const mcpToolLabel = page.locator("label").filter({
      has: page.getByText(TOOL, { exact: true }),
    });
    await mcpToolLabel.getByRole("checkbox").check();
    await page.getByLabel("Read-only mount").uncheck();
    await page
      .locator("label")
      .filter({ has: page.getByText("Allows writes", { exact: true }) })
      .getByRole("checkbox")
      .check();
    await page.getByLabel("Output path template").fill(OUTPUT);
    await page.getByLabel("Output format", { exact: true }).selectOption("markdown");
    await page.getByLabel("Verify checks").fill("output_exists");
    await page.getByRole("button", { name: "Add check" }).click();
    await page.getByLabel("Finalizer").fill("ready_for_workflow_output_verification");
    await page.getByRole("button", { name: "Create draft" }).click();

    const draftReview = page.getByRole("region", { name: `${workflowName} draft review` });
    await expect(draftReview.getByText("Simulation ok", { exact: true })).toBeVisible({
      timeout: 60_000,
    });
    await expect(draftReview.getByText("No validation findings.", { exact: true })).toBeVisible();
    await draftReview.getByRole("button", { name: "Approve", exact: true }).click();
    await expect(draftReview.getByRole("button", { name: "Approved", exact: true })).toBeVisible({
      timeout: 60_000,
    });

    await page.reload();
    const card = page.locator("article[data-workflow-id]").filter({ hasText: workflowName });
    await expect(card).toBeVisible({ timeout: 60_000 });
    await expect(card.locator('[data-status="approved"]')).toBeVisible();
    await expect(card.getByLabel(`${workflowName} tool surface`)).toContainText(qualified);
    await expect(card.getByLabel(`${workflowName} MCP mounts`)).toContainText(serverName);

    const reviews = await getJson<{ workflows: WorkflowReview[] }>(
      request,
      `${AGENT_API}/api/workflows`,
    );
    const review = reviews.workflows.find((item) => item.name === workflowName);
    expect(review).toBeTruthy();
    expect(review?.approved).toBe(true);
    expect(review?.validation_findings).toEqual([]);
    expect(review?.compiled_surface.compiled).toBe(true);
    expect(review?.compiled_surface.allowed_tools).toEqual(
      expect.arrayContaining([qualified, "file_write", "finish", "needs_input", "skip"]),
    );
    expect(review?.compiled_surface.advertised_tools).toEqual(
      review?.compiled_surface.allowed_tools,
    );

    // Start via the real Run button and open the product-created Agent route.
    await card.getByRole("button", { name: "Run", exact: true }).click();
    const runLink = card.locator('a[href^="/agent/"]');
    await expect(runLink).toBeVisible({ timeout: 60_000 });
    cid = decodeURIComponent((await runLink.getAttribute("href"))!.split("/").at(-1)!);
    await runLink.click();
    await expect(page).toHaveURL(new RegExp(`/agent/${cid}$`));
    await waitForStatus(request, cid, new Set(["FINISHED", "VERIFIED"]), 900_000);

    const events = await allEvents(request, cid);
    const actions = events.filter((event) => event.kind === "action");
    const calledTools = actions.map((event) => event.tool_call?.tool_name).filter(Boolean);
    expect(calledTools.filter((name) => name === qualified)).toHaveLength(1);
    expect(calledTools).toContain("file_write");
    expect(
      events.some(
        (event) =>
          event.kind === "observation" &&
          event.tool_result?.tool_name === qualified &&
          event.tool_result.success === true &&
          String(event.tool_result.content ?? "").includes(RESULT),
      ),
      "no successful MCP observation contained the fixture proof marker",
    ).toBe(true);
    const allowed = new Set(review!.compiled_surface.allowed_tools);
    expect(calledTools.filter((name) => !allowed.has(String(name)))).toEqual([]);

    const archive = await request.get(`${AGENT_API}/api/projects/${cid}/download`);
    expect(archive.ok(), await archive.text()).toBe(true);
    const zipPath = testInfo.outputPath(`${cid}.zip`);
    fs.writeFileSync(zipPath, await archive.body());
    const output = await import("node:child_process").then(({ execFileSync }) =>
      execFileSync("unzip", ["-p", zipPath, OUTPUT], { encoding: "utf-8", timeout: 30_000 }),
    );
    expect(output).toContain(RESULT);
    expect(output).toContain(TOKEN);

    if (fixture.callsUrl) {
      const calls = await getJson<{ calls: Array<{ method?: string; params?: Record<string, unknown> }> }>(
        request,
        fixture.callsUrl,
      );
      const toolCalls = calls.calls.filter(
        (call) => call.method === "tools/call" && call.params?.name === TOOL,
      );
      expect(toolCalls).toHaveLength(1);
      expect((toolCalls[0].params?.arguments as Record<string, unknown>)?.token).toBe(TOKEN);
    }

    assertNoThrash(events, await inspectTrace(request, cid));
  } finally {
    if (cid) {
      await deleteConversation(request, cid);
    }
    // Remove through the same Settings path so runtime removal/reload is also
    // exercised. Cleanup failures remain visible because leaked global config
    // would invalidate later repetitions on the isolated stack.
    await removeMcpThroughSettings(page, serverName);
    await stopMcpFixture(fixture);
  }
});
