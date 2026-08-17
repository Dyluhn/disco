import { expect, test } from "@playwright/test";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

// ESM module scope — no __dirname; derive it (frontend package is "type": "module").
const __dirname = path.dirname(fileURLToPath(import.meta.url));

/**
 * BP-06 rung 5 (live, inspection-only — DRIVES NO BUILD): observation masking is
 * strictly LLM-view-side. The activity feed of a FINISHED real build must render
 * the FULL original content of an old, large tool observation (one the LLM view
 * would have stubbed: >= 600 chars with >= 8 newer observations), and the stub
 * literal `[masked output:` must appear nowhere in the UI.
 *
 * Target conversation: $PMX_SPEC_CID pin (the BP-06 rung-4 EE-Quest run) or a
 * snapshot-of-known-ids scan — never newest-first limit=1.
 *
 * Evidence: test-record/screenshots/bp-06/feed-unmasked.png +
 *           test-record/bp-06/feed-unmasked-witness.json
 */

const API = "http://127.0.0.1:8000";
const SCREENSHOT_DIR = path.resolve(__dirname, "../../test-record/screenshots/bp-06");
const RECORD_DIR = path.resolve(__dirname, "../../test-record/bp-06");

// Command observations arrive as BOTH `shell` (one-off) and `shell_exec` (session).
const COMMAND_TOOLS = new Set(["shell", "shell_exec"]);

type ToolCall = { tool_name?: string; arguments?: Record<string, unknown> };
type ToolResult = { tool_name?: string; content?: string };
type EventJson = {
  seq?: number;
  kind?: string;
  tool_call?: ToolCall;
  tool_result?: ToolResult;
};

async function fetchJson(request: import("@playwright/test").APIRequestContext, url: string) {
  const res = await request.get(url, { timeout: 15_000 });
  if (!res.ok()) throw new Error(`GET ${url} -> ${res.status()}: ${await res.text()}`);
  return res.json();
}

/** Full paginated event log (limit=100 pages, ordered by seq). */
async function allEvents(
  request: import("@playwright/test").APIRequestContext,
  cid: string,
): Promise<EventJson[]> {
  const events: EventJson[] = [];
  let after = 0;
  for (let page = 0; page < 100; page++) {
    const body = await fetchJson(
      request,
      `${API}/conversations/${cid}/events?limit=100&after_seq=${after}`,
    );
    const batch: EventJson[] = body.events ?? [];
    if (batch.length === 0) break;
    events.push(...batch);
    after = Math.max(...batch.map((e) => e.seq ?? after));
  }
  return events;
}

/** The witness: FIRST observation >= 600 chars with >= 8 later observations. */
function findWitness(events: EventJson[]) {
  const obs = events.filter((e) => e.kind === "observation" && e.tool_result);
  for (let i = 0; i < obs.length; i++) {
    const content = obs[i].tool_result?.content ?? "";
    if (content.length >= 600 && obs.length - 1 - i >= 8) return obs[i];
  }
  return undefined;
}

/** A 40-char probe from `at`, scanning forward past whitespace-only slices. */
function probeAt(content: string, at: number): string {
  for (let start = at; start + 40 <= content.length; start += 40) {
    const p = content.slice(start, start + 40).trim();
    if (p.length >= 12) return p;
  }
  return content.slice(at, at + 40).trim();
}

/** Mirror ActivityFeed's argsPreview so we can find the witness row by text. */
function argsPreview(toolName: string, args: Record<string, unknown>): string {
  if (toolName === "shell") return String(args.command ?? "");
  if (toolName === "search") return `"${String(args.query ?? "")}"`;
  if (toolName === "extract") return String(args.url ?? "");
  if (toolName.startsWith("file_")) return String(args.path ?? "");
  const argText = JSON.stringify(args);
  return argText.length > 80 ? argText.slice(0, 80) + "…" : argText;
}

test("feed renders FULL old large observation; masking stub never leaks (live)", async ({
  page,
  request,
}) => {
  test.setTimeout(300_000); // inspection-only: well under 5 minutes

  // 1. Backend health — skip, never crash, when the agent-server is down.
  let healthy = false;
  try {
    const res = await request.get(`${API}/health`, { timeout: 5_000 });
    healthy = res.ok();
  } catch {
    healthy = false;
  }
  test.skip(!healthy, `agent-server not reachable at ${API} — start it and re-run`);

  // 2. Resolve the target conversation: env pin, else snapshot-of-known-ids scan.
  let cid = process.env.PMX_SPEC_CID ?? "";
  let witness: EventJson | undefined;
  if (cid) {
    witness = findWitness(await allEvents(request, cid));
    expect(witness, `pinned ${cid} has no maskable witness observation`).toBeTruthy();
  } else {
    const known: string[] =
      (await fetchJson(request, `${API}/conversations?limit=25`)).conversation_ids ?? [];
    for (const candidate of known) {
      const state = await fetchJson(request, `${API}/conversations/${candidate}/state`);
      if (state.execution_status !== "FINISHED") continue;
      const w = findWitness(await allEvents(request, candidate));
      if (w) {
        cid = candidate;
        witness = w;
        break;
      }
    }
    test.skip(
      !witness,
      "no FINISHED conversation among the known 25 has a >=600-char observation with >=8 later observations",
    );
  }
  const content = witness!.tool_result!.content!;
  const witnessTool = witness!.tool_result!.tool_name ?? "";
  const startProbe = probeAt(content, 0);
  const midProbe = probeAt(content, Math.floor(content.length / 2));
  expect(startProbe, "start probe came up empty — content unusable").toBeTruthy();

  // 3. Open the build through the real UI (Resume route) and wait for feed rows.
  await page.goto(`/build/${cid}`, { timeout: 30_000 });
  const rows = page.locator("button", { has: page.locator("span.truncate") });
  await expect(rows.first(), "no feed rows rendered").toBeVisible({ timeout: 30_000 });

  // 4. Find + expand the witness's row. Preferred: match the row by the preview
  //    text ActivityFeed derives from the PAIRED action's arguments (the action is
  //    the nearest preceding kind=="action" with the same tool name — accepting
  //    shell/shell_exec interchangeably for command observations).
  const events = await allEvents(request, cid);
  const wseq = witness!.seq ?? 0;
  const pairedAction = [...events]
    .filter((e) => e.kind === "action" && (e.seq ?? 0) < wseq && e.tool_call)
    .reverse()
    .find((e) => {
      const t = e.tool_call?.tool_name ?? "";
      return COMMAND_TOOLS.has(witnessTool)
        ? COMMAND_TOOLS.has(t)
        : t === witnessTool;
    });

  const outputPre = page
    .locator("pre")
    .filter({ hasText: startProbe.slice(0, 30) });

  let expanded = false;
  if (pairedAction?.tool_call) {
    const preview = argsPreview(
      pairedAction.tool_call.tool_name ?? "",
      pairedAction.tool_call.arguments ?? {},
    );
    const needle = preview.slice(0, 30).trim();
    if (needle) {
      const matches = rows.filter({ hasText: needle });
      const n = Math.min(await matches.count(), 20);
      for (let i = 0; i < n && !expanded; i++) {
        // dispatchEvent, not click(): the feed's sticky header (plan progress,
        // z-10) intercepts pointer events for rows scrolled beneath it.
        await matches.nth(i).dispatchEvent("click");
        expanded = (await outputPre.count()) > 0;
      }
    }
  }
  // Fallback (acceptable per brief): expand rows in order until the Output pre
  // holding the start probe appears.
  const total = await rows.count();
  for (let i = 0; i < total && !expanded; i++) {
    await rows.nth(i).dispatchEvent("click"); // same sticky-header dodge as above
    expanded = (await outputPre.count()) > 0;
  }
  expect(expanded, `no expanded row's Output <pre> contains the witness start probe`).toBeTruthy();

  // 5. FULL content in the DOM: both probes present in the same <pre>.
  const preText = (await outputPre.first().textContent()) ?? "";
  expect(preText, "witness Output <pre> lost the start of the content").toContain(startProbe);
  expect(preText, "witness Output <pre> is truncated — middle of content missing").toContain(
    midProbe,
  );

  // 6. The LLM-view stub never leaks into the UI.
  const bodyText = (await page.locator("body").textContent()) ?? "";
  expect(bodyText, "masking stub leaked into the user-facing feed").not.toContain(
    "[masked output:",
  );

  // 7. Evidence.
  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  fs.mkdirSync(RECORD_DIR, { recursive: true });
  await page.screenshot({
    path: path.join(SCREENSHOT_DIR, "feed-unmasked.png"),
    fullPage: true,
  });
  fs.writeFileSync(
    path.join(RECORD_DIR, "feed-unmasked-witness.json"),
    JSON.stringify(
      { cid, seq: witness!.seq, tool_name: witnessTool, contentLength: content.length },
      null,
      2,
    ),
  );
});
