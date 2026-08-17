import { expect, test, type APIRequestContext, type TestInfo } from "@playwright/test";
import * as fs from "node:fs";

import {
  AGENT_API,
  authenticatedMutation,
  deleteConversation,
  requireReliabilityStack,
  restartReliabilityStack,
} from "./reliability-helpers";

const TIMEZONE = "America/Chicago";
const CRON = "0 9 * * 1";
const DESCRIPTION = "every Monday at 9:00 AM";

test.use({ timezoneId: TIMEZONE, trace: "off" });

type ScheduleRow = {
  schedule_id: string;
  conversation_id: string;
  rrule: string;
  description: string;
  timezone: string;
  enabled: boolean;
  next_run: string | null;
};

async function createIdleBuild(request: APIRequestContext): Promise<string> {
  const response = await authenticatedMutation(request, `${AGENT_API}/conversations`, {
    data: {
      surface: "build",
      title: "F06 Chicago schedule reliability",
      autonomous: false,
      assist: false,
    },
    timeout: 30_000,
  });
  expect(response.ok(), await response.text()).toBe(true);
  return String((await response.json()).conversation_id);
}

async function listSchedules(
  request: APIRequestContext,
  cid: string,
): Promise<ScheduleRow[]> {
  const response = await request.get(
    `${AGENT_API}/api/conversations/${encodeURIComponent(cid)}/schedules`,
    { timeout: 30_000 },
  );
  expect(response.ok(), await response.text()).toBe(true);
  return ((await response.json()) as { schedules: ScheduleRow[] }).schedules;
}

function assertChicagoMondayAtNine(value: string): void {
  expect(Number.isFinite(Date.parse(value)), "schedule returned an invalid timestamp").toBe(true);
  const formatter = new Intl.DateTimeFormat("en-US", {
    timeZone: TIMEZONE,
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  });
  const parts = Object.fromEntries(
    formatter
      .formatToParts(new Date(value))
      .filter((part) => part.type !== "literal")
      .map((part) => [part.type, part.value]),
  );
  expect(parts.weekday).toBe("Mon");
  expect(parts.hour).toBe("09");
  expect(parts.minute).toBe("00");
}

function assertChicagoMondaysAtNine(nextRuns: string[]): void {
  expect(nextRuns).toHaveLength(3);
  const epoch = nextRuns.map((value) => Date.parse(value));
  expect(epoch.every(Number.isFinite), "preview returned an invalid timestamp").toBe(true);
  expect(epoch[0]).toBeLessThan(epoch[1]);
  expect(epoch[1]).toBeLessThan(epoch[2]);
  for (const value of nextRuns) assertChicagoMondayAtNine(value);
}

function assertPersistedRow(row: ScheduleRow, cid: string): void {
  expect(row.conversation_id).toBe(cid);
  expect(row.rrule).toBe(CRON);
  expect(row.description).toBe(DESCRIPTION);
  expect(row.timezone).toBe(TIMEZONE);
  expect(row.enabled).toBe(true);
  expect(row.next_run).toBeTruthy();
  assertChicagoMondayAtNine(row.next_run!);
}

test("Weekly Mon 9am remains local and human-readable across restart", async ({
  page,
  request,
}, testInfo: TestInfo) => {
  test.setTimeout(300_000);
  await requireReliabilityStack(request);
  expect(
    await page.evaluate(() => Intl.DateTimeFormat().resolvedOptions().timeZone),
    "Firefox did not start in the required schedule timezone",
  ).toBe(TIMEZONE);

  const cid = await createIdleBuild(request);
  let scheduleId: string | null = null;
  try {
    await page.goto(`/build/${cid}`);
    const section = page.locator('section[aria-labelledby="schedule-heading"]');
    await expect(section.getByRole("heading", { name: "Schedules" })).toBeVisible({
      timeout: 60_000,
    });
    await expect(section.getByText("No schedules yet. Create one to automate recurring runs.")).toBeVisible();
    await section.getByRole("button", { name: "New schedule" }).click();

    const previewRequestPromise = page.waitForRequest(
      (candidate) =>
        candidate.method() === "POST" &&
        new URL(candidate.url()).pathname.endsWith("/api/schedules/preview"),
    );
    const previewResponsePromise = page.waitForResponse(
      (candidate) =>
        candidate.request().method() === "POST" &&
        new URL(candidate.url()).pathname.endsWith("/api/schedules/preview"),
    );
    await section.getByRole("button", { name: "Weekly Mon 9am" }).click();
    const [previewRequest, previewResponse] = await Promise.all([
      previewRequestPromise,
      previewResponsePromise,
    ]);
    expect(previewRequest.postDataJSON()).toEqual({
      rrule: CRON,
      timezone: TIMEZONE,
      n: 3,
    });
    expect(previewResponse.ok(), await previewResponse.text()).toBe(true);
    const preview = (await previewResponse.json()) as {
      rrule: string;
      timezone: string;
      next_runs: string[];
    };
    expect(preview.rrule).toBe(CRON);
    expect(preview.timezone).toBe(TIMEZONE);
    assertChicagoMondaysAtNine(preview.next_runs);

    await expect(section.getByText(DESCRIPTION)).toBeVisible();
    await expect(section.getByText(TIMEZONE)).toBeVisible();
    await expect(section.getByText(CRON, { exact: true })).toBeVisible();
    await section.screenshot({ path: testInfo.outputPath("f06-before-save.png") });

    const createPath = `/api/conversations/${cid}/schedules`;
    const createRequestPromise = page.waitForRequest(
      (candidate) =>
        candidate.method() === "POST" && new URL(candidate.url()).pathname.endsWith(createPath),
    );
    const createResponsePromise = page.waitForResponse(
      (candidate) =>
        candidate.request().method() === "POST" &&
        new URL(candidate.url()).pathname.endsWith(createPath),
    );
    await section.getByRole("button", { name: "Save schedule" }).click();
    const [createRequest, createResponse] = await Promise.all([
      createRequestPromise,
      createResponsePromise,
    ]);
    expect(createRequest.postDataJSON()).toEqual({
      rrule: CRON,
      description: DESCRIPTION,
      timezone: TIMEZONE,
    });
    expect(createResponse.ok(), await createResponse.text()).toBe(true);
    const created = (await createResponse.json()) as ScheduleRow;
    scheduleId = created.schedule_id;
    assertPersistedRow(created, cid);

    await expect(section.getByText(DESCRIPTION)).toBeVisible();
    await expect(section.getByText(TIMEZONE)).toBeVisible();
    await expect(section.getByText(CRON, { exact: true })).toBeVisible();
    await section.screenshot({ path: testInfo.outputPath("f06-saved-row.png") });

    const beforeRestart = await listSchedules(request, cid);
    expect(beforeRestart).toHaveLength(1);
    assertPersistedRow(beforeRestart[0], cid);

    await restartReliabilityStack();
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
    await page.reload();
    await expect(section.getByText(DESCRIPTION)).toBeVisible({ timeout: 60_000 });
    await expect(section.getByText(TIMEZONE)).toBeVisible();
    await expect(section.getByText(CRON, { exact: true })).toBeVisible();
    await section.screenshot({ path: testInfo.outputPath("f06-post-restart.png") });

    const afterRestart = await listSchedules(request, cid);
    expect(afterRestart).toHaveLength(1);
    assertPersistedRow(afterRestart[0], cid);
    expect(afterRestart[0].schedule_id).toBe(scheduleId);

    fs.writeFileSync(
      testInfo.outputPath("f06-schedule-timezone-evidence.json"),
      JSON.stringify(
        {
          revision: process.env.DISCO_RELIABILITY_REVISION ?? null,
          browser_timezone: TIMEZONE,
          conversation_id: cid,
          schedule_id: scheduleId,
          preview_request: previewRequest.postDataJSON(),
          preview_response: preview,
          create_request: createRequest.postDataJSON(),
          before_restart: beforeRestart[0],
          after_restart: afterRestart[0],
          fire_now_called: false,
        },
        null,
        2,
      ),
      "utf-8",
    );
  } finally {
    try {
      if (scheduleId) {
        const deleted = await authenticatedMutation(
          request,
          `${AGENT_API}/api/schedules/${scheduleId}`,
          {
            method: "DELETE",
            timeout: 30_000,
          },
        );
        expect(deleted.ok(), await deleted.text()).toBe(true);
        expect((await deleted.json()).deleted, "schedule cleanup did not delete its row").toBe(
          true,
        );
      }
    } finally {
      await deleteConversation(request, cid);
    }
  }
});
