import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

import {
  AGENT_API,
  allEvents,
  assertNoThrash,
  conversationIdFromWebSocket,
  deleteConversation,
  inspectTrace,
  requireReliabilityStack,
} from "./reliability-helpers";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const PYTHON = process.env.PMX_VENV_PY ?? path.join(REPO, ".venv/bin/python3");
const QUESTION =
  "Explain how WebAssembly component-model interfaces improve portable server plugins, " +
  "using a few concrete primary web sources.";

type AudioDone = {
  stage: "done";
  mp3_url: string;
  transcript_url: string;
};

function sseEvents(body: string): Array<Record<string, unknown>> {
  const events: Array<Record<string, unknown>> = [];
  for (const frame of body.split(/\r?\n\r?\n/)) {
    const line = frame.split(/\r?\n/).find((candidate) => candidate.startsWith("data:"));
    if (!line) continue;
    events.push(JSON.parse(line.slice(5).trim()) as Record<string, unknown>);
  }
  return events;
}

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
      { cwd: REPO, encoding: "utf-8", timeout: 240_000 },
    );
  } catch (error) {
    const failure = error as { stdout?: string; stderr?: string };
    throw new Error(`report oracle failed:\n${failure.stdout ?? ""}\n${failure.stderr ?? ""}`);
  }
}

function validateMp3(file: string): Record<string, unknown> {
  let raw = "";
  try {
    raw = execFileSync(
      "ffprobe",
      [
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name:format=duration",
        "-of",
        "json",
        file,
      ],
      { encoding: "utf-8", timeout: 60_000 },
    );
  } catch (error) {
    throw new Error(`ffprobe could not decode generated audio: ${String(error)}`);
  }
  const probe = JSON.parse(raw) as {
    streams?: Array<{ codec_name?: string }>;
    format?: { duration?: string };
  };
  expect(probe.streams?.[0]?.codec_name).toBe("mp3");
  expect(Number(probe.format?.duration ?? 0), "generated MP3 has no duration").toBeGreaterThan(1);
  return probe as Record<string, unknown>;
}

test("a new grounded report renders real synthesis progress and exports playable audio", async ({
  page,
  request,
}, testInfo) => {
  test.setTimeout(2_400_000);
  await requireReliabilityStack(request);

  const reusedCid = process.env.DISCO_RELIABILITY_AUDIO_CID?.trim() ?? "";
  let cid = reusedCid;
  page.on("websocket", (socket) => {
    cid ||= conversationIdFromWebSocket(socket.url()) ?? "";
  });

  try {
    if (reusedCid) {
      // Diagnostic/resume path: reopen a completed report in a brand-new UI
      // process without paying for another model run. Promotion never sets this
      // variable, so its normal lane still proves fresh report production.
      await page.goto(`/deep/${encodeURIComponent(reusedCid)}`);
      await expect(page.locator('[data-dr-phase="done"]')).toBeVisible({ timeout: 120_000 });
    } else {
      await page.goto("/");
      await page.getByRole("button", { name: /^scope:/i }).click();
      await page.getByRole("menuitem", { name: /deep research/i }).click();
      await page.locator('[data-disco-control="dr.depth-tier"]').click();
      await page.getByRole("menuitem", { name: /quick/i }).click();
      const input = page.getByPlaceholder(/ask a research question/i);
      await input.fill(QUESTION);
      await input.press("Enter");
      // v2 is gateless: submitting starts the research. Wait on the model's
      // brief (the first visible output) instead of an approval gate.
      await page
        .locator("[data-dr-brief]")
        .waitFor({ state: "visible", timeout: 300_000 });
      await expect(page.locator('[data-dr-phase="done"]')).toBeVisible({
        timeout: 1_500_000,
      });
      expect(cid, "Deep Research WebSocket did not identify its conversation").toBeTruthy();
    }

    const events = await allEvents(request, cid);
    const report = events.filter((event) => event.kind === "report").at(-1) as
      | Record<string, unknown>
      | undefined;
    expect(report, "audio run had no authoritative ReportEvent").toBeTruthy();
    const reportQuery = String(report?.query ?? "");
    if (!reusedCid) expect(reportQuery).toBe(QUESTION);
    validateReport(
      report as Record<string, unknown>,
      testInfo.outputPath("report-event.json"),
      testInfo.outputPath("report-oracle.json"),
    );

    // The conversation is new, so this cid-scoped audio artifact cannot already
    // exist. Require the streaming production path and its genuine stages; a
    // cache_hit or blocking fallback is not accepted as synthesis evidence.
    const streamResponsePromise = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response.url().includes(`/conversations/${cid}/report/audio/stream?mode=single`),
      { timeout: 60_000 },
    );
    await page.getByRole("button", { name: /^Audio Overview$/ }).click();
    const single = page.locator('[data-disco-control="dr.audio.single"]');
    await single.waitFor({ state: "visible", timeout: 30_000 });

    const uiStagesPromise = (async (): Promise<string[]> => {
      const seen = new Set<string>();
      const marker = page.locator('[data-tts-state]').first();
      const deadline = Date.now() + 900_000;
      while (Date.now() < deadline) {
        const state = (await marker.getAttribute("data-tts-state")) ?? "";
        const stage = (await marker.getAttribute("data-tts-stage")) ?? "";
        if (stage) seen.add(stage);
        if (state === "done") return [...seen];
        if (state === "unavailable") {
          throw new Error(`audio UI became unavailable: ${await marker.textContent()}`);
        }
        await page.waitForTimeout(100);
      }
      throw new Error("audio UI never reached done");
    })();

    await single.click();
    const streamResponse = await streamResponsePromise;
    expect(streamResponse.ok(), `audio stream returned ${streamResponse.status()}`).toBe(true);
    await expect(page.locator('[data-tts-state="done"]')).toBeVisible({ timeout: 900_000 });
    const renderedStages = await uiStagesPromise;
    expect(
      renderedStages.some((stage) =>
        ["preparing", "downloading_model", "synthesizing", "mixing"].includes(stage),
      ),
      `UI rendered no real audio progress stage: ${renderedStages.join(", ")}`,
    ).toBe(true);

    const streamEvents = sseEvents(await streamResponse.text());
    const stages = streamEvents.map((event) => String(event.stage ?? ""));
    expect(stages).toContain("preparing");
    expect(stages).toContain("synthesizing");
    expect(stages).toContain("mixing");
    expect(stages).not.toContain("cache_hit");
    const synthesis = streamEvents.filter((event) => event.stage === "synthesizing");
    expect(synthesis.length).toBeGreaterThan(0);
    expect(
      synthesis.every(
        (event) => Number(event.current ?? 0) > 0 && Number(event.total ?? 0) >= Number(event.current),
      ),
    ).toBe(true);
    const done = streamEvents.find((event) => event.stage === "done") as AudioDone | undefined;
    expect(done?.mp3_url).toMatch(/\.mp3$/);
    expect(done?.transcript_url).toMatch(/\.md$/);

    const audioSrc = await page.locator('audio[aria-hidden="true"]').getAttribute("src");
    expect(audioSrc).toBeTruthy();
    // Same-origin deployments intentionally expose agent routes through a
    // relative /svc/agent proxy. Resolve like the browser instead of requiring
    // an absolute src attribute.
    const renderedAudioUrl = new URL(audioSrc as string, page.url());
    expect(renderedAudioUrl.pathname.endsWith(String(done?.mp3_url))).toBe(true);

    const mp3Response = await request.get(`${AGENT_API}${done?.mp3_url}`, { timeout: 120_000 });
    expect(mp3Response.ok()).toBe(true);
    expect(mp3Response.headers()["content-type"]).toContain("audio/mpeg");
    const mp3 = await mp3Response.body();
    expect(mp3.length, "audio endpoint returned a tiny/empty artifact").toBeGreaterThan(10_000);
    const hasMpegHeader =
      mp3.subarray(0, 3).toString("ascii") === "ID3" ||
      mp3.subarray(0, 4_096).some((byte, index, bytes) => byte === 0xff && (bytes[index + 1] & 0xe0) === 0xe0);
    expect(hasMpegHeader, "audio bytes have no ID3 or MPEG frame header").toBe(true);

    const transcriptResponse = await request.get(`${AGENT_API}${done?.transcript_url}`, {
      timeout: 120_000,
    });
    expect(transcriptResponse.ok()).toBe(true);
    expect(transcriptResponse.headers()["content-type"]).toContain("text/markdown");
    const transcript = await transcriptResponse.text();
    expect(transcript.length).toBeGreaterThan(300);
    expect(transcript).toContain("# Audio Overview Transcript");
    expect(transcript).toContain(`Query: ${reportQuery}`);
    expect(transcript).toMatch(/Turns: [1-9][0-9]*/);

    const downloadPromise = page.waitForEvent("download");
    await page.locator('[data-disco-control="dr.audio.export"]').click();
    const download = await downloadPromise;
    const downloadPath = testInfo.outputPath(download.suggestedFilename());
    await download.saveAs(downloadPath);
    expect(fs.readFileSync(downloadPath).equals(mp3), "UI export differs from served MP3 bytes").toBe(true);
    const ffprobe = validateMp3(downloadPath);

    let trace: Record<string, unknown>;
    try {
      const inspect = await inspectTrace(request, cid);
      // Deep Research drives the routed phase engine directly, not AgentLoop, so
      // it truthfully has routing decisions but no synthetic `agent.step` span.
      // Keep every event/repair/repetition check while using that surface contract.
      assertNoThrash(events, inspect, { requireCompletedAgentStep: false });
      trace = inspect;
    } catch (error) {
      // A diagnostic reuse may target a report created before the current
      // agent-server process, so its in-memory inspect trace is legitimately
      // gone. Promotion never reuses a cid and must still fail hard here.
      if (!reusedCid) throw error;
      expect(
        events.some((event) => event.kind === "status" && event.status === "STUCK"),
      ).toBe(false);
      trace = { unavailable_for_reused_conversation: true };
    }
    fs.writeFileSync(
      testInfo.outputPath("audio-evidence.json"),
      JSON.stringify(
        { cid, stages, renderedStages, mp3Bytes: mp3.length, transcript, ffprobe, trace },
        null,
        2,
      ),
      "utf-8",
    );
  } finally {
    if (cid && !reusedCid) {
      await deleteConversation(request, cid);
    }
  }
});
