import { expect } from "@playwright/test";
import type { APIRequestContext, TestInfo } from "@playwright/test";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";

import {
  AGENT_API,
  allEvents,
  assertNoThrash,
  authenticatedMutation,
  deleteConversation,
  inspectTrace,
  waitForStatus,
} from "./reliability-helpers";

const FILE_MARKER = "AGENT_GENERAL_TASK_PROOF";
const SCRIPT_MARKER = "AGENT_TERMINAL_SCRIPT_PROOF";

async function createAgent(request: APIRequestContext, title: string): Promise<string> {
  const response = await authenticatedMutation(request, `${AGENT_API}/conversations`, {
    data: { surface: "agent", title, autonomous: true, assist: false },
  });
  expect(response.ok(), await response.text()).toBe(true);
  return String((await response.json()).conversation_id);
}

async function sendLegacyFirstFrame(
  request: APIRequestContext,
  cid: string,
  content: string,
): Promise<void> {
  // A pre-fix UI always included this marker. Agent must ignore it so stale
  // clients cannot reactivate Build's web contract.
  const response = await authenticatedMutation(
    request,
    `${AGENT_API}/conversations/${cid}/messages`,
    { data: { content, build_brief: {} }, timeout: 30_000 },
  );
  expect(response.ok(), await response.text()).toBe(true);
}

/** Compact general-task cells folded into the existing Agent live lane so the
 * frozen live-suite identity remains stable. */
export async function runAgentGeneralTaskCells(
  request: APIRequestContext,
  testInfo: TestInfo,
): Promise<void> {
  const conversations: string[] = [];
  const evidence: Record<string, unknown> = {};
  let passed = false;

  try {
    const directCid = await createAgent(request, "Agent direct-answer task");
    conversations.push(directCid);
    await sendLegacyFirstFrame(
      request,
      directCid,
      "What is 37 times 41? Give the answer with a one-sentence explanation.",
    );
    await waitForStatus(request, directCid, new Set(["FINISHED", "VERIFIED"]), 600_000);

    const directEvents = await allEvents(request, directCid);
    const directTools = directEvents
      .filter((event) => event.kind === "action")
      .map((event) => String(event.tool_call?.tool_name ?? ""));
    const buildOnlyTools = new Set([
      "file_write",
      "file_append",
      "serve",
      "preview_start",
      "verify_web_app",
      "verify_appkit_app",
      "design_lint",
      "scaffold_starter",
    ]);
    expect(directTools.filter((tool) => buildOnlyTools.has(tool))).toEqual([]);
    expect(
      directEvents.some(
        (event) =>
          event.kind === "message" &&
          event.source === "agent" &&
          String(event.message?.content ?? "").includes("1517"),
      ),
      "Agent never returned the direct answer in its user-facing result",
    ).toBe(true);
    expect(
      directEvents.some(
        (event) =>
          event.source === "environment" &&
          Boolean((event.meta as Record<string, unknown> | undefined)?.build_brief),
      ),
      "legacy Agent ingress persisted a Build brief",
    ).toBe(false);
    expect(
      directEvents.some((event) => event.kind === "build_platform_admission"),
      "plain Agent was provisionally admitted as a Build target",
    ).toBe(false);
    const directTrace = await inspectTrace(request, directCid);
    assertNoThrash(directEvents, directTrace);
    evidence.direct = { conversation_id: directCid, events: directEvents, trace: directTrace };

    const fileCid = await createAgent(request, "Agent file-artifact task");
    conversations.push(fileCid);
    await sendLegacyFirstFrame(
      request,
      fileCid,
      `Create notes/agent-proof.md containing the exact marker ${FILE_MARKER} and ` +
        "a concise three-item checklist for reviewing meeting notes. Hand me the file.",
    );
    await waitForStatus(request, fileCid, new Set(["FINISHED", "VERIFIED"]), 600_000);

    const fileEvents = await allEvents(request, fileCid);
    const fileTools = fileEvents
      .filter((event) => event.kind === "action")
      .map((event) => String(event.tool_call?.tool_name ?? ""));
    expect(fileTools).toContain("file_write");
    const deliverables = fileEvents.filter((event) => event.kind === "deliverable");
    expect(deliverables).toHaveLength(1);
    expect(deliverables[0]?.path).toBe("notes/agent-proof.md");
    expect(fileEvents.some((event) => event.kind === "build_platform_admission")).toBe(false);
    expect(
      fileTools.filter((tool) =>
        new Set(["preview_start", "verify_web_app", "verify_appkit_app", "design_lint"]).has(
          tool,
        ),
      ),
    ).toEqual([]);

    const archive = await request.get(`${AGENT_API}/api/projects/${fileCid}/download`);
    expect(archive.ok(), await archive.text()).toBe(true);
    const zipPath = testInfo.outputPath(`${fileCid}.zip`);
    fs.writeFileSync(zipPath, await archive.body());
    const output = execFileSync("unzip", ["-p", zipPath, "notes/agent-proof.md"], {
      encoding: "utf-8",
      timeout: 30_000,
    });
    expect(output).toContain(FILE_MARKER);
    const fileTrace = await inspectTrace(request, fileCid);
    assertNoThrash(fileEvents, fileTrace);
    evidence.file = { conversation_id: fileCid, events: fileEvents, trace: fileTrace };

    const scriptCid = await createAgent(request, "Agent terminal-script task");
    conversations.push(scriptCid);
    await sendLegacyFirstFrame(
      request,
      scriptCid,
      `Create tools/primes.py as a terminal script containing the marker ${SCRIPT_MARKER}. ` +
        "It must print exactly: 2, 3, 5, 7, 11. Run it once, then hand me the Python file.",
    );
    await waitForStatus(request, scriptCid, new Set(["FINISHED", "VERIFIED"]), 600_000);

    const scriptEvents = await allEvents(request, scriptCid);
    const scriptActions = scriptEvents.filter((event) => event.kind === "action");
    const scriptTools = scriptActions.map((event) => String(event.tool_call?.tool_name ?? ""));
    expect(scriptTools).toContain("file_write");
    const commandTools = new Set(["shell", "shell_exec", "code_exec"]);
    const runActions = scriptActions.filter(
      (event) =>
        commandTools.has(String(event.tool_call?.tool_name ?? "")) &&
        JSON.stringify(event.tool_call?.arguments ?? {}).includes("tools/primes.py"),
    );
    expect(runActions.length, "Agent did not execute the requested terminal script").toBeGreaterThan(0);
    const runActionIds = new Set(runActions.map((event) => String(event.id)));
    expect(
      scriptEvents.some(
        (event) =>
          event.kind === "observation" &&
          runActionIds.has(String(event.action_id)) &&
          event.tool_result?.success === true &&
          String(event.tool_result?.content ?? "").includes("2, 3, 5, 7, 11"),
      ),
      "terminal-script execution did not produce the requested output",
    ).toBe(true);
    const scriptDeliverables = scriptEvents.filter((event) => event.kind === "deliverable");
    expect(scriptDeliverables).toHaveLength(1);
    expect(scriptDeliverables[0]?.path).toBe("tools/primes.py");
    expect(scriptDeliverables[0]?.artifact_kind).toBe("files");
    expect(scriptEvents.some((event) => event.kind === "build_platform_admission")).toBe(false);
    expect(
      scriptTools.filter((tool) =>
        new Set(["preview_start", "verify_web_app", "verify_appkit_app", "design_lint"]).has(
          tool,
        ),
      ),
    ).toEqual([]);

    const scriptArchive = await request.get(`${AGENT_API}/api/projects/${scriptCid}/download`);
    expect(scriptArchive.ok(), await scriptArchive.text()).toBe(true);
    const scriptZipPath = testInfo.outputPath(`${scriptCid}.zip`);
    fs.writeFileSync(scriptZipPath, await scriptArchive.body());
    const scriptOutput = execFileSync("unzip", ["-p", scriptZipPath, "tools/primes.py"], {
      encoding: "utf-8",
      timeout: 30_000,
    });
    expect(scriptOutput).toContain(SCRIPT_MARKER);
    const scriptTrace = await inspectTrace(request, scriptCid);
    assertNoThrash(scriptEvents, scriptTrace);
    evidence.script = { conversation_id: scriptCid, events: scriptEvents, trace: scriptTrace };

    fs.writeFileSync(
      testInfo.outputPath("agent-general-task-trace.json"),
      JSON.stringify(evidence, null, 2),
    );
    passed = true;
  } finally {
    // Preserve a failed conversation so its exact model-facing trace remains
    // available for root-cause analysis. Successful cells clean themselves up.
    if (passed) {
      for (const cid of conversations.reverse()) {
        await deleteConversation(request, cid);
      }
    }
  }
}
