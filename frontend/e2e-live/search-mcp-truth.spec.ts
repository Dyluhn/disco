import { expect, test, type WebSocket } from "@playwright/test";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

import {
  addMcpThroughSettings,
  approveMcpUntilConnected,
  removeMcpThroughSettings,
  startMcpFixture,
  stopMcpFixture,
} from "./mcp-reliability-helpers";
import {
  AGENT_API,
  allEvents,
  deleteConversation,
  getJson,
  inspectTrace,
  requireReliabilityStack,
} from "./reliability-helpers";

// This file lives at `current/frontend/e2e-live/`, so the repo root is THREE
// levels up; `../..` landed on `current/`, whose `.venv/bin/python3` does not
// exist, and the spawn then failed with an ENOENT carrying no status and no
// output. `harness` is not an installed distribution either — pytest reaches it
// through `pythonpath = [".", "development", "current"]` in pyproject.toml,
// which is pytest-only, so a subprocess needs `development` on PYTHONPATH.
const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
const PYTHON = process.env.PMX_VENV_PY ?? path.join(REPO, ".venv/bin/python3");
const ORACLE_PYTHONPATH = [path.join(REPO, "development"), process.env.PYTHONPATH]
  .filter(Boolean)
  .join(path.delimiter);
const SOURCE_URL = "https://www.rfc-editor.org/rfc/rfc9110.txt";
const MARKER = "DISCO_MCP_SEARCH_PROOF";
const QUESTION =
  `For reliability marker ${MARKER}, explain RFC 9110's definition of safe HTTP methods ` +
  "and give its examples. Use inline citations to specific sources.";

type GroundedAnswer = {
  passages?: Array<{ source_url?: string; source_engine?: string }>;
  [key: string]: unknown;
};

function parseFrame(payload: string | Buffer): Record<string, unknown> | null {
  try {
    return JSON.parse(payload.toString()) as Record<string, unknown>;
  } catch {
    return null;
  }
}

function validateAnswer(answer: GroundedAnswer, input: string, output: string): void {
  fs.writeFileSync(input, JSON.stringify(answer, null, 2), "utf-8");
  try {
    execFileSync(
      PYTHON,
      [
        "-m",
        "harness.reliability.search_oracles",
        "answer",
        input,
        "--probe-connectivity",
        "--output",
        output,
      ],
      {
        cwd: REPO,
        encoding: "utf-8",
        timeout: 180_000,
        env: { ...process.env, PYTHONPATH: ORACLE_PYTHONPATH },
      },
    );
  } catch (error) {
    const failure = error as { stdout?: string; stderr?: string };
    throw new Error(`MCP grounding oracle failed:\n${failure.stdout ?? ""}\n${failure.stderr ?? ""}`);
  }
}

test("an approved MCP search provider hot-loads and produces a connected grounded citation", async ({
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
  expect(
    fixture.callsUrl,
    "Search MCP proof requires the deterministic fixture call ledger",
  ).toBeTruthy();
  const suffix = `${process.pid}_${testInfo.workerIndex}_${testInfo.repeatEachIndex}_${Date.now()}`;
  const serverName = `reliability_search_${suffix}`;
  const qualifiedSearch = `mcp__${serverName}__search`;
  const qualifiedFetch = `mcp__${serverName}__fetch`;
  const answers: GroundedAnswer[] = [];
  const conversationIds = new Set<string>();
  const pageErrors: Error[] = [];
  const streamErrors: string[] = [];
  let terminalSettled = false;
  let rejectBeforeTerminal: (error: Error) => void = () => undefined;
  const earlyFailure = new Promise<never>((_resolve, reject) => {
    rejectBeforeTerminal = reject;
  });
  void earlyFailure.catch(() => undefined);

  page.on("pageerror", (error) => {
    pageErrors.push(error);
    if (!terminalSettled) rejectBeforeTerminal(error);
  });

  page.on("websocket", (socket: WebSocket) => {
    if (!socket.url().includes("/ws/research")) return;
    socket.on("framesent", (frame) => {
      const cid = parseFrame(frame.payload)?.conversation_id;
      if (typeof cid === "string" && cid) conversationIds.add(cid);
    });
    socket.on("framereceived", (frame) => {
      const payload = parseFrame(frame.payload);
      if (payload?.type === "error") {
        const message = String(payload.message ?? "unknown Search stream error");
        streamErrors.push(message);
        if (!terminalSettled) rejectBeforeTerminal(new Error(message));
      }
      if (payload?.type === "final" && typeof payload.answer === "object") {
        answers.push(payload.answer as GroundedAnswer);
      }
    });
  });

  try {
    await page.goto("/settings");
    await addMcpThroughSettings(page, { name: serverName, url: fixture.mcpUrl });
    await approveMcpUntilConnected(page, serverName);

    const authoring = await getJson<{
      mcp_servers: Array<{ server: string; tools: string[] }>;
    }>(request, `${AGENT_API}/api/workflows/authoring-context`);
    const mounted = authoring.mcp_servers.find((item) => item.server === serverName);
    expect(mounted?.tools).toEqual(expect.arrayContaining(["search", "fetch"]));

    await page.goto("/");
    const input = page.getByLabel("Ask Disco a question");
    await input.fill(QUESTION);
    await input.press("Enter");
    await Promise.race([
      page
        .locator('[data-research-phase="done"][data-stream-state="final"]')
        .waitFor({ state: "visible", timeout: 600_000 }),
      earlyFailure,
    ]);
    terminalSettled = true;
    expect(pageErrors, "Search crashed the browser surface").toEqual([]);
    expect(streamErrors, "Search emitted a terminal error frame").toEqual([]);
    expect(answers, "Search emitted no authoritative final answer").toHaveLength(1);
    fs.writeFileSync(
      testInfo.outputPath("mcp-answer-raw.json"),
      JSON.stringify(answers[0], null, 2),
      "utf-8",
    );
    expect(
      answers[0].passages?.some((passage) => passage.source_url === SOURCE_URL),
      "the MCP-only RFC result never became a citable passage",
    ).toBe(true);
    validateAnswer(
      answers[0],
      testInfo.outputPath("mcp-answer.json"),
      testInfo.outputPath("mcp-answer-oracle.json"),
    );

    expect(conversationIds.size).toBe(1);
    const cid = [...conversationIds][0];
    const trace = await inspectTrace(request, cid);
    expect(trace.routing_decisions?.length ?? 0, "MCP Search model-routing trace is empty").toBeGreaterThan(0);
    const events = await allEvents(request, cid);
    expect(events.some((event) => event.kind === "status" && event.status === "STUCK")).toBe(false);

    const evidence = await getJson<{
      runtime?: {
        mcp_retrieval_searches?: string[];
        mcp_retrieval_extractions?: string[];
      };
    }>(request, `${AGENT_API}/api/debug/evidence/${cid}`);
    expect(evidence.runtime?.mcp_retrieval_searches).toContain(qualifiedSearch);
    expect(evidence.runtime?.mcp_retrieval_extractions).toContain(qualifiedFetch);

    const calls = await getJson<{
      calls: Array<{ method?: string; params?: Record<string, unknown> }>;
    }>(request, fixture.callsUrl as string);
    const searchCalls = calls.calls.filter(
      (call) => call.method === "tools/call" && call.params?.name === "search",
    );
    expect(searchCalls.length, "running Search never invoked the MCP search tool").toBeGreaterThan(0);
    const identical = new Map<string, number>();
    for (const call of searchCalls) {
      const key = JSON.stringify(call.params?.arguments ?? {});
      identical.set(key, (identical.get(key) ?? 0) + 1);
    }
    expect(
      Math.max(...identical.values()),
      "Search thrashed on an identical MCP invocation",
    ).toBeLessThanOrEqual(2);
    fs.writeFileSync(
      testInfo.outputPath("mcp-calls.json"),
      JSON.stringify(calls, null, 2),
      "utf-8",
    );

    fs.writeFileSync(
      testInfo.outputPath("mcp-search-evidence.json"),
      JSON.stringify({ cid, answer: answers[0], trace, runtime: evidence.runtime }, null, 2),
      "utf-8",
    );
  } finally {
    for (const cid of conversationIds) {
      await deleteConversation(request, cid);
    }
    await removeMcpThroughSettings(page, serverName);
    await stopMcpFixture(fixture);
  }
});
