import { expect, test, type WebSocket } from "@playwright/test";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

import {
  deleteConversation,
  inspectTrace,
  requireReliabilityStack,
} from "./reliability-helpers";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const PYTHON = process.env.PMX_VENV_PY ?? path.join(REPO, ".venv/bin/python3");
const QUESTION =
  "Using specific web sources, explain what the WebAssembly Component Model is, " +
  "what problem it solves, and its current practical limitations.";

type GroundedAnswer = {
  query?: string;
  passages?: Array<{ source_url?: string }>;
  follow_ups?: string[];
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
      { cwd: REPO, encoding: "utf-8", timeout: 180_000 },
    );
  } catch (error) {
    const failure = error as { stdout?: string; stderr?: string };
    throw new Error(
      `grounding oracle failed:\n${failure.stdout ?? ""}\n${failure.stderr ?? ""}`,
    );
  }
}

test("Search returns connected, resolvable, graded citations and a working follow-up", async ({
  page,
  request,
}, testInfo) => {
  test.setTimeout(1_200_000);
  await requireReliabilityStack(request);

  const answers: GroundedAnswer[] = [];
  const conversationIds = new Set<string>();
  page.on("websocket", (socket: WebSocket) => {
    if (!socket.url().includes("/ws/research")) return;
    socket.on("framesent", (frame) => {
      const payload = parseFrame(frame.payload);
      const cid = payload?.conversation_id;
      if (typeof cid === "string" && cid) conversationIds.add(cid);
    });
    socket.on("framereceived", (frame) => {
      const payload = parseFrame(frame.payload);
      if (payload?.type === "final" && typeof payload.answer === "object") {
        answers.push(payload.answer as GroundedAnswer);
      }
    });
  });

  try {
    await page.goto("/");
    const input = page.getByLabel("Ask Disco a question");
    await input.fill(QUESTION);
    await input.press("Enter");
    await expect(
      page.locator('[data-research-phase="done"][data-stream-state="final"]'),
    ).toBeVisible({ timeout: 600_000 });
    expect(answers.length, "WebSocket never delivered the authoritative final answer").toBe(1);

    // UI truth: cited sources are actually rendered and link out.
    const citedTab = page.getByRole("tab", { name: /cited/i });
    await expect(citedTab).toBeVisible();
    await citedTab.click();
    await expect(page.locator('a[href^="http"]').first()).toBeVisible();

    validateAnswer(
      answers[0],
      testInfo.outputPath("first-answer.json"),
      testInfo.outputPath("first-oracle.json"),
    );

    // Follow the product-generated suggestion through the UI. A second final
    // frame with the new query proves this is a real new retrieval, not a dead
    // chip or stale rendering of answer one.
    const followUpText = answers[0].follow_ups?.[0];
    expect(followUpText, "answer contained no first follow-up").toBeTruthy();
    await page.locator('[data-disco-control="search.followup"]').first().click();
    await expect(page.locator('[data-research-phase="running"]')).toBeVisible({
      timeout: 60_000,
    });
    await expect(
      page.locator('[data-research-phase="done"][data-stream-state="final"]'),
    ).toBeVisible({ timeout: 600_000 });
    expect(answers.length).toBe(2);
    expect(answers[1].query).toBe(followUpText);
    expect(JSON.stringify(answers[1])).not.toBe(JSON.stringify(answers[0]));
    validateAnswer(
      answers[1],
      testInfo.outputPath("follow-up-answer.json"),
      testInfo.outputPath("follow-up-oracle.json"),
    );

    expect(conversationIds.size, "search did not attach its model run to a conversation").toBe(1);
    const cid = [...conversationIds][0];
    const trace = await inspectTrace(request, cid);
    expect(trace.routing_decisions?.length ?? 0, "search model-routing trace is empty").toBeGreaterThan(0);
    fs.writeFileSync(
      testInfo.outputPath("search-inspect-trace.json"),
      JSON.stringify(trace, null, 2),
      "utf-8",
    );
  } finally {
    for (const cid of conversationIds) {
      await deleteConversation(request, cid);
    }
  }
});
