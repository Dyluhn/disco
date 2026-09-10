import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

import {
  allEvents,
  assertNoThrash,
  conversationIdFromWebSocket,
  deleteConversation,
  inspectTrace,
  requireReliabilityStack,
} from "./reliability-helpers";

// This file lives at `frontend/e2e-live/`, so the repo root is two levels up.
//
//
//
//
//
const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const PYTHON = process.env.PMX_VENV_PY ?? path.join(REPO, ".venv/bin/python3");
const QUESTION =
  "Compare the practical security and portability tradeoffs of WebAssembly runtimes " +
  "for server-side plugin systems, using specific primary web sources.";
const FOLLOW_UP = "Which two limitations matter most for a small self-hosted product, and why?";

function validateReport(report: Record<string, unknown>, input: string, output: string): void {
  fs.writeFileSync(input, JSON.stringify(report, null, 2), "utf-8");
  try {
    execFileSync(
      PYTHON,
      [
        "-m",
        "harness.reliability.search_oracles",
        "report",
        input,
        "--probe-connectivity",
        "--output",
        output,
      ],
      {
        cwd: REPO,
        encoding: "utf-8",
        timeout: 240_000,
      },
    );
  } catch (error) {
    // A killed child (the 240 s cap) reports empty stdout AND stderr, so the
    // status/signal have to be in the message or the failure says nothing at
    // all — which is exactly what it used to say.
    const failure = error as {
      code?: string | null;
      message?: string | null;
      status?: number | null;
      signal?: string | null;
      stdout?: string | null;
      stderr?: string | null;
    };
    // A spawn-level failure (ENOENT, EACCES) and a maxBuffer overrun both carry
    // NEITHER status nor signal, so `code` and `message` have to be in here too
    // — without them this wall reports "unknown" and the reader is no better off
    // than with the empty string it used to print.
    const how =
      failure.signal != null
        ? `killed by ${failure.signal} (the ${240_000} ms cap)`
        : failure.status != null
          ? `exit status ${failure.status}`
          : `no exit status — spawn/buffer failure, code=${failure.code ?? "none"}`;
    throw new Error(
      `report oracle failed — ${how}\n${failure.message ?? ""}\n` +
        `stdout:\n${failure.stdout ?? ""}\nstderr:\n${failure.stderr ?? ""}`,
    );
  }
}

test("Deep Research report is grounded, resumes, answers a follow-up, and exports real bytes", async ({
  context,
  page,
  request,
}, testInfo) => {
  test.setTimeout(2_400_000);
  await requireReliabilityStack(request);

  let cid = "";
  page.on("websocket", (socket) => {
    cid ||= conversationIdFromWebSocket(socket.url()) ?? "";
  });

  try {
    // The composer is a RADIOGROUP ("Search" / "Deep Research") with the depth
    // tier behind the `dr.options` disclosure. The old flow here clicked a
    // `scope:` button into a menu that no longer exists, so this spec's FIRST
    // action failed on every nightly run. Same selectors the S1 evidence specs
    // drive.
    await page.goto("/");
    await page.getByRole("radio", { name: /deep research/i }).click();
    const input = page.getByPlaceholder(/ask a research question/i);
    await input.waitFor({ state: "visible", timeout: 30_000 });
    await page.locator('[data-disco-control="dr.options"]').click();
    await page.locator('[data-disco-control="dr.depth-tier"]').click();
    await page.getByRole("menuitem", { name: /^Quick/ }).click();
    await page.keyboard.press("Escape");
    await input.fill(QUESTION);
    await input.press("Enter");
    // v2 is gateless: submitting starts the research. The model's brief is the
    // first visible output — waiting on it proves the run really began instead
    // of silently sitting idle for the whole report timeout.
    await page
      .locator("[data-dr-brief]")
      .waitFor({ state: "visible", timeout: 300_000 });
    await expect(page.locator('[data-dr-phase="done"]')).toBeVisible({ timeout: 1_500_000 });
    expect(cid, "Deep Research WebSocket did not identify its conversation").toBeTruthy();

    let events = await allEvents(request, cid);
    const reports = events.filter((event) => event.kind === "report");
    expect(reports.length, "FINISHED deep run emitted no ReportEvent").toBeGreaterThan(0);
    const report = reports.at(-1) as Record<string, unknown>;
    validateReport(
      report,
      testInfo.outputPath("report-event.json"),
      testInfo.outputPath("report-oracle.json"),
    );
    await expect(page.getByRole("tab", { name: /cited/i })).toBeVisible();

    // Close and reopen the application on the durable route before continuing.
    await page.close();
    page = await context.newPage();
    await page.goto(`/deep/${cid}`);
    await expect(page.locator('[data-dr-phase="done"]')).toBeVisible({ timeout: 120_000 });
    await expect(page.getByRole("tab", { name: /cited/i })).toBeVisible();

    const reportSeq = Number(report.seq ?? -1);
    await page.getByRole("button", { name: "Ask a Follow-Up" }).click();
    // The textarea, not the form that wraps it: `getByLabel` matches a substring,
    // and the form's accessible name is "Ask a follow-up question about the
    // report", so the bare label resolved to two elements and failed strict mode.
    await page.getByRole("textbox", { name: "Follow-up question" }).fill(FOLLOW_UP);
    await page.locator('[data-disco-control="dr.follow-up"]').click();

    const followUpDeadline = Date.now() + 600_000;
    let answer = "";
    let followUpSeq = -1;
    let ledger: Record<string, unknown> | null = null;
    while (Date.now() < followUpDeadline) {
      events = await allEvents(request, cid);
      const afterReport = events.filter((event) => Number(event.seq ?? -1) > reportSeq);
      const questionIndex = afterReport.findIndex(
        (event) => event.kind === "message" && event.message?.role === "user" && event.message.content === FOLLOW_UP,
      );
      if (questionIndex >= 0) {
        followUpSeq = Number(afterReport[questionIndex].seq ?? -1);
        const afterQuestion = afterReport.slice(questionIndex + 1);
        const response = afterQuestion.find(
          (event) => event.kind === "message" && event.message?.role === "assistant",
        );
        answer = response?.message?.content ?? "";
        // The grounding ledger the server appends immediately before the answer.
        ledger =
          (afterQuestion.find(
            (event) =>
              event.kind === "action" &&
              (event as { tool_call?: { tool_name?: string } }).tool_call?.tool_name ===
                "follow_up_grounding",
          ) as { tool_call?: { arguments?: Record<string, unknown> } } | undefined)
            ?.tool_call?.arguments ?? null;
        if (answer) break;
      }
      await page.waitForTimeout(1_000);
    }
    // A length check cannot tell an answer from a refusal: the old
    // `answer.length > 80` passed on a 128-character refusal that had replaced
    // the whole answer, and the citation assertion below then failed with no
    // hint of why. The ledger is the server's own count, so assert on that.
    expect(ledger, "follow-up answer carried no grounding ledger").not.toBeNull();
    expect(ledger?.refused, "follow-up was refused, not answered").toBe(false);
    expect(
      Number(ledger?.supported ?? 0) + Number(ledger?.weak ?? 0),
      "no statement of the follow-up answer survived grounding",
    ).toBeGreaterThan(0);
    expect(answer, "the answer is the source-coverage refusal").not.toContain(
      "may not cover the question",
    );
    expect(answer.length, "follow-up produced no persisted assistant answer").toBeGreaterThan(80);
    // The report view renders the question as `<span>Follow-up: </span>{question}`,
    // so the paragraph's full text is "Follow-up: …" and `exact` never matched.
    await expect(page.getByText(FOLLOW_UP).first()).toBeVisible({ timeout: 60_000 });

    const passageIds = new Set(
      ((report.passages as Array<{ id?: string }>) ?? [])
        .map((passage) => passage.id)
        .filter((id): id is string => Boolean(id)),
    );
    const answerCitations = [...answer.matchAll(/\[\[([\w-]+)\]\]/g)].map((match) => match[1]);
    expect(answerCitations.length, "follow-up answer contained no inline citations").toBeGreaterThan(0);
    expect(answerCitations.every((passageId) => passageIds.has(passageId))).toBe(true);

    // Download MD with the follow-up included, then PDF, and validate their
    // actual signatures/content rather than trusting a toast.
    let downloadPromise = page.waitForEvent("download");
    await page.locator('[data-disco-control="dr.export.topbar.md"]').click();
    await page.locator('[data-disco-control="dr.include-followups.confirm"]').click();
    let download = await downloadPromise;
    const mdPath = testInfo.outputPath(await download.suggestedFilename());
    await download.saveAs(mdPath);
    const markdown = fs.readFileSync(mdPath, "utf-8");
    expect(markdown.length).toBeGreaterThan(500);
    expect(markdown).toContain(QUESTION);
    expect(markdown).toContain(FOLLOW_UP);

    const pdfButton = page.locator('[data-disco-control="dr.export.topbar.pdf"]');
    expect(await pdfButton.getAttribute("data-export-cap"), "PDF capability unavailable").toBe("true");
    downloadPromise = page.waitForEvent("download");
    await pdfButton.click();
    await page.locator('[data-disco-control="dr.include-followups.confirm"]').click();
    download = await downloadPromise;
    const pdfPath = testInfo.outputPath(await download.suggestedFilename());
    await download.saveAs(pdfPath);
    const pdf = fs.readFileSync(pdfPath);
    expect(pdf.length).toBeGreaterThan(1_000);
    expect(pdf.subarray(0, 5).toString("ascii")).toBe("%PDF-");

    const trace = await inspectTrace(request, cid);
    // Deep Research drives its own loop (retrieval/deep_research/agent.py), not
    // core's AgentLoop, so the trace carries routing decisions but never an
    // `agent.step` span — same as the audio spec.
    assertNoThrash(events, trace, { requireCompletedAgentStep: false });
    fs.writeFileSync(
      testInfo.outputPath("deep-research-evidence.json"),
      JSON.stringify({ cid, reportSeq, followUpSeq, answer, events, trace }, null, 2),
      "utf-8",
    );
  } finally {
    if (cid) {
      await deleteConversation(request, cid);
    }
  }
});
