import { expect, test, type Page, type WebSocket } from "@playwright/test";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";

import {
  AGENT_API,
  allEvents,
  assertNoThrash,
  authenticatedMutation,
  conversationState,
  deleteConversation,
  getJson,
  inspectTrace,
  requireReliabilityStack,
  waitForStatus,
} from "./reliability-helpers";

const OUTPUT = "browser-proof.md";
const MARKER = "GVISOR_NOVNC_TASK_PROOF";

function streamSocket(page: Page): Promise<WebSocket> {
  return new Promise((resolve) => {
    page.on("websocket", (socket) => {
      if (/websockify|6080/i.test(socket.url())) resolve(socket);
    });
  });
}

async function bounded<T>(promise: Promise<T>, timeoutMs: number, label: string): Promise<T> {
  return Promise.race([
    promise,
    new Promise<T>((_resolve, reject) =>
      setTimeout(() => reject(new Error(`${label} timed out after ${timeoutMs}ms`)), timeoutMs),
    ),
  ]);
}

async function waitForFrame(socket: WebSocket): Promise<void> {
  await bounded(
    new Promise<void>((resolve) => socket.once("framereceived", () => resolve())),
    60_000,
    "noVNC WebSocket frame",
  );
}

async function waitForBrowserReady(
  request: import("@playwright/test").APIRequestContext,
  cid: string,
): Promise<void> {
  await expect
    .poll(
      async () => {
        const response = await request.get(
          `${AGENT_API}/conversations/${cid}/browser/live-ready`,
          { timeout: 10_000 },
        );
        if (!response.ok()) return `http-${response.status()}`;
        const body = (await response.json()) as { ready?: boolean; reason?: string };
        return body.ready ? "ready" : String(body.reason ?? "not-ready");
      },
      { timeout: 300_000, intervals: [500, 1_000, 2_000] },
    )
    .toBe("ready");
}

async function assertStreaming(page: Page, socketPromise: Promise<WebSocket>): Promise<WebSocket> {
  const iframe = page.getByTestId("novnc-iframe");
  await expect(iframe).toBeVisible({ timeout: 120_000 });
  expect(await iframe.getAttribute("sandbox")).toContain("allow-scripts");
  expect(await iframe.getAttribute("sandbox")).not.toContain("allow-same-origin");
  const src = String(await iframe.getAttribute("src"));
  expect(src).toMatch(/6080/);
  expect(src).toMatch(/view_only=1|preview_bootstrap/i);
  expect(src).not.toMatch(/(?:10|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d+\.\d+/);
  await expect(page.getByTestId("live-badge")).toHaveAttribute("data-streaming", "true", {
    timeout: 120_000,
  });

  const socket = await bounded(socketPromise, 120_000, "proxied noVNC WebSocket");
  expect(socket.url()).toMatch(/websockify|6080/i);
  expect(socket.url()).not.toMatch(/(?:10|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d+\.\d+/);
  await waitForFrame(socket);
  return socket;
}

test("gVisor browser task streams through jailed noVNC, reconnects, and leaves no orphan", async ({
  context,
  page,
  request,
}, testInfo) => {
  test.setTimeout(1_200_000);
  expect(
    /^(1|true|yes)$/i.test(process.env.DISCO_RELIABILITY_GVISOR ?? ""),
    "set DISCO_RELIABILITY_GVISOR=1 only for a real gVisor + noVNC stack",
  ).toBe(true);
  await requireReliabilityStack(request);

  const health = await getJson<{ reachable: boolean; backend: string; detail?: string }>(
    request,
    `${AGENT_API}/api/sandbox/health`,
  );
  expect(health.reachable, health.detail).toBe(true);
  expect(health.backend).toBe("gvisor");

  const create = await authenticatedMutation(request, `${AGENT_API}/conversations`, {
    data: {
      surface: "agent",
      title: "Reliability gVisor noVNC lifecycle",
      autonomous: true,
      assist: false,
    },
  });
  expect(create.ok(), await create.text()).toBe(true);
  const cid = String((await create.json()).conversation_id);
  let capturedInstanceIds: string[] = [];

  try {
    // Mount the resume route before kicking so the UI witnesses browser startup,
    // rather than inspecting a conversation after the useful lifecycle ended.
    const firstSocket = streamSocket(page);
    await page.goto(`/agent/${cid}`);
    const kick = await authenticatedMutation(
      request,
      `${AGENT_API}/conversations/${cid}/messages`,
      {
      data: {
        content:
          "Use the browser tool (not search or extract) to open https://example.com and " +
          "inspect its title and visible heading. After the browser opens, run shell command " +
          "sleep 75 so the live view remains observable. Then write browser-proof.md with " +
          `the exact marker ${MARKER}, the URL, title, and heading, and finish.`,
      },
      },
    );
    expect(kick.ok(), await kick.text()).toBe(true);

    await expect
      .poll(
        async () => {
          const state = await conversationState(request, cid);
          capturedInstanceIds = Array.isArray(
            (state.extras as Record<string, unknown> | undefined)?.sandbox_instance_ids,
          )
            ? ((state.extras as Record<string, unknown>).sandbox_instance_ids as string[])
            : [];
          return {
            backend: state.sandbox_backend,
            hasInstance: capturedInstanceIds.length > 0,
            hasBrowserAction: (await allEvents(request, cid)).some(
              (event) => event.kind === "action" && event.tool_call?.tool_name === "browser",
            ),
          };
        },
        { timeout: 300_000, intervals: [1_000, 2_000] },
      )
      .toEqual({ backend: "gvisor", hasInstance: true, hasBrowserAction: true });

    await waitForBrowserReady(request, cid);
    const firstStream = await assertStreaming(page, firstSocket);

    // The server response is capability metadata only: view-only path and port,
    // never a raw upstream address that bypasses the conversation jail.
    const live = await authenticatedMutation(
      request,
      `${AGENT_API}/conversations/${cid}/browser/live-url`,
    );
    expect(live.ok(), await live.text()).toBe(true);
    const liveBody = (await live.json()) as Record<string, unknown>;
    expect(liveBody).toMatchObject({ ready: true, port: 6080 });
    expect(String(liveBody.novnc_path)).toContain("view_only=1");
    expect(liveBody).not.toHaveProperty("url");
    expect(JSON.stringify(liveBody)).not.toMatch(/192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|10\./);

    const touch = await authenticatedMutation(
      request,
      `${AGENT_API}/conversations/${cid}/browser/live-touch`,
    );
    expect(touch.ok(), await touch.text()).toBe(true);
    expect((await touch.json()).ok).toBe(true);

    // Closing the app tears down the pane. The stop endpoint must remain
    // idempotent, and reopening the same durable Agent route must establish a
    // fresh proxied WebSocket while the browser task is still running.
    await page.close();
    const stopOne = await authenticatedMutation(
      request,
      `${AGENT_API}/conversations/${cid}/browser/live-stop`,
    );
    const stopTwo = await authenticatedMutation(
      request,
      `${AGENT_API}/conversations/${cid}/browser/live-stop`,
    );
    expect(stopOne.ok()).toBe(true);
    expect(stopTwo.ok()).toBe(true);
    await expect.poll(() => firstStream.isClosed(), { timeout: 30_000 }).toBe(true);

    page = await context.newPage();
    const secondSocket = streamSocket(page);
    await page.goto(`/agent/${cid}`);
    await waitForBrowserReady(request, cid);
    await assertStreaming(page, secondSocket);

    // The model still has to perform the promised task; streaming alone cannot
    // pass this suite.
    await waitForStatus(request, cid, new Set(["FINISHED", "VERIFIED"]), 900_000);
    const events = await allEvents(request, cid);
    const browserActions = events.filter(
      (event) => event.kind === "action" && event.tool_call?.tool_name === "browser",
    );
    expect(browserActions.length).toBeGreaterThanOrEqual(1);
    expect(
      events.some(
        (event) =>
          event.kind === "observation" &&
          event.tool_result?.tool_name === "browser" &&
          event.tool_result.success === true &&
          /example\.com/i.test(String(event.tool_result.structured?.url ?? event.tool_result.content)),
      ),
    ).toBe(true);

    const archive = await request.get(`${AGENT_API}/api/projects/${cid}/download`);
    expect(archive.ok(), await archive.text()).toBe(true);
    const zipPath = testInfo.outputPath(`${cid}.zip`);
    fs.writeFileSync(zipPath, await archive.body());
    const output = execFileSync("unzip", ["-p", zipPath, OUTPUT], {
      encoding: "utf-8",
      timeout: 30_000,
    });
    expect(output).toContain(MARKER);
    expect(output).toMatch(/Example Domain/i);
    assertNoThrash(events, await inspectTrace(request, cid));

    const killed = await authenticatedMutation(
      request,
      `${AGENT_API}/conversations/${cid}/kill`,
    );
    expect(killed.ok(), await killed.text()).toBe(true);
    expect((await killed.json()).killed).toBe(true);
    await expect
      .poll(
        async () => {
          const evidence = await getJson<{
            runtime?: {
              sandbox_instance_ids?: string[];
              live_session?: boolean;
              sandbox_state?: string | null;
            };
          }>(request, `${AGENT_API}/api/debug/evidence/${cid}`);
          return {
            instances: evidence.runtime?.sandbox_instance_ids?.length ?? 0,
            live: evidence.runtime?.live_session ?? false,
          };
        },
        { timeout: 60_000 },
      )
      .toEqual({ instances: 0, live: false });
    expect(capturedInstanceIds.length, "no real sandbox instance id was ever observed").toBeGreaterThan(0);
  } finally {
    if (!page.isClosed()) await page.close().catch(() => undefined);
    await authenticatedMutation(
      request,
      `${AGENT_API}/conversations/${cid}/browser/live-stop`,
    ).catch(() => undefined);
    await deleteConversation(request, cid);
  }
});
