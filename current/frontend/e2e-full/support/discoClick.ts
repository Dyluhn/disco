/**
 * discoClick — click wrapper with full evidence provenance.
 *
 * Records before-screenshot, accessible name/role/selector/route/disabled,
 * performs the click, takes an after-screenshot, then optionally waits for
 * declared backend evidence (WS frame type, HTTP status, event-log entries).
 * Every call appends a record to ui-actions.jsonl.
 *
 * evidence-harness-campaign.md W12
 */

import * as fs from "node:fs";
import * as path from "node:path";
import type { Locator, Page } from "@playwright/test";
import type { Recorder } from "./recorder";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** Options controlling backend-evidence assertions after a click. */
export interface DiscoClickOpts {
  /**
   * WS frame type to wait for (matched against `payload.type` inside
   * websockets.jsonl, written by wsRecorder).  e.g. "plan_approved".
   */
  expectWs?: string;
  /**
   * HTTP response status code the next page response must carry.
   * Uses `page.waitForResponse` set up BEFORE the click.
   */
  expectStatus?: number;
  /**
   * Event `kind` values (the `EventKind` discriminator, e.g. `"action"`,
   * `"observation"`, `"status"`, `"deliverable"`) that must appear in the
   * conversation event log at  GET /conversations/{cid}/events  within the
   * poll timeout.  These map to the `kind` field the agent-server serializes
   * via pydantic `model_dump` — see events.py `EventKind` enum.
   * Requires `agentBaseUrl` + `cid`.
   */
  expectEvents?: string[];
  /** Agent-server root, e.g. "http://127.0.0.1:8000". Required for expectEvents. */
  agentBaseUrl?: string;
  /** Conversation ID to poll for event-log evidence. Required for expectEvents. */
  cid?: string;
  /** Optional override for the ui_action_id (defaults to a timestamp+counter slug). */
  actionId?: string;
}

/** One record appended to ui-actions.jsonl per discoClick call. */
export interface UiActionRecord {
  schema: "ui-action";
  ui_action_id: string;
  ts_before: string;
  ts_after: string;
  accessible_name: string;
  role: string;
  selector: string;
  /** Page URL at time of click. */
  route: string;
  disabled: boolean;
  /** Path relative to the run folder. */
  screenshot_before: string;
  /** Path relative to the run folder. */
  screenshot_after: string;
  backend_evidence: BackendEvidence;
}

interface BackendEvidence {
  ws_type_waited?: string;
  ws_found?: boolean;
  status_waited?: number;
  status_found?: boolean;
  events_waited?: string[];
  events_found?: boolean;
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

let _counter = 0;

function makeActionId(): string {
  return `action-${Date.now()}-${(++_counter).toString().padStart(4, "0")}`;
}

/**
 * Poll `websockets.jsonl` until a WS frame whose `payload.type` equals
 * `frameType` appears, or until `timeoutMs` elapses.
 *
 * The file is written synchronously by wsRecorder so polling it is safe.
 */
async function pollWsType(
  filePath: string,
  frameType: string,
  timeoutMs = 10_000,
): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (fs.existsSync(filePath)) {
      const text = fs.readFileSync(filePath, "utf-8");
      for (const line of text.split("\n")) {
        if (!line.trim()) continue;
        try {
          const obj = JSON.parse(line) as {
            event?: string;
            payload?: Record<string, unknown>;
          };
          if (
            (obj.event === "framesent" || obj.event === "framereceived") &&
            obj.payload?.["type"] === frameType
          ) {
            return true;
          }
        } catch {
          // malformed line — skip
        }
      }
    }
    await new Promise<void>((r) => setTimeout(r, 200));
  }
  return false;
}

/**
 * Poll GET `url` (JSON object `{events: [...], next_cursor}`) until every event
 * kind in `kinds` appears as `kind` in at least one record.
 *
 * The real discriminator field the agent-server serializes is `kind` (events.py,
 * pydantic model_dump).  `event_type` / `type` are kept as fallback aliases for
 * forward-compat only.
 *
 * Follows pagination via `next_cursor` on each sweep.
 */
async function pollEventLog(
  url: string,
  kinds: string[],
  timeoutMs = 15_000,
): Promise<boolean> {
  const remaining = new Set(kinds);
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      // Fetch all pages in this sweep using next_cursor pagination
      let nextCursor: number | null = null;
      let isFirstPage = true;
      do {
        const pageUrl = isFirstPage
          ? url
          : `${url}?after_seq=${nextCursor as number}`;
        isFirstPage = false;
        const res = await fetch(pageUrl);
        if (!res.ok) break;
        const obj = (await res.json()) as {
          events?: unknown[];
          next_cursor?: number | null;
        };
        const events = Array.isArray(obj.events) ? obj.events : [];
        for (const ev of events) {
          const row = ev as Record<string, unknown>;
          // `kind` is the real field; fall back to legacy aliases only.
          const t = row["kind"] ?? row["event_type"] ?? row["type"];
          if (typeof t === "string") remaining.delete(t);
        }
        nextCursor =
          typeof obj.next_cursor === "number" ? obj.next_cursor : null;
      } while (nextCursor !== null && Date.now() < deadline);

      if (remaining.size === 0) return true;
    } catch {
      // network error — keep polling
    }
    await new Promise<void>((r) => setTimeout(r, 500));
  }
  return false;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Click `locator` with full click-level evidence provenance.
 *
 * Steps:
 *  1. Before-screenshot saved to screenshots/before-{actionId}.png
 *  2. Read accessible name, role, selector slug, current URL, disabled state
 *  3. If `opts.expectStatus` given — arm `page.waitForResponse` BEFORE click
 *  4. Perform `locator.click()`
 *  5. After-screenshot saved to screenshots/after-{actionId}.png
 *  6. Resolve any declared backend-evidence watchers (WS / HTTP status / event log)
 *  7. Append {@link UiActionRecord} to ui-actions.jsonl
 */
export async function discoClick(
  page: Page,
  recorder: Recorder,
  locator: Locator,
  opts: DiscoClickOpts = {},
): Promise<void> {
  const actionId = opts.actionId ?? makeActionId();
  const screenshotsDir = path.join(recorder.runDir, "screenshots");
  fs.mkdirSync(screenshotsDir, { recursive: true });

  // -------------------------------------------------------------------------
  // 1 + 2: before-click evidence
  // -------------------------------------------------------------------------
  const tsBefore = new Date().toISOString();
  const beforeFile = `before-${actionId}.png`;
  await page.screenshot({
    path: path.join(screenshotsDir, beforeFile),
    fullPage: false,
  });

  // Accessible name: aria-label → text content → empty string
  const ariaLabel = await locator.getAttribute("aria-label");
  const textContent = ariaLabel ?? (await locator.textContent())?.trim() ?? "";

  // Role: role attribute → tag name (via structural evaluate — no DOM lib needed)
  const roleAttr = await locator.getAttribute("role");
  const tagName = await locator.evaluate(
    (el: { tagName: string }) => el.tagName.toLowerCase(),
  );
  const role = roleAttr ?? tagName;

  // Selector slug: data-testid → data-disco-control → aria-label → tag name
  const testId = await locator.getAttribute("data-testid");
  const discoControl = await locator.getAttribute("data-disco-control");
  const selector = testId ?? discoControl ?? ariaLabel ?? tagName;

  const route = page.url();

  // Disabled state
  const disabledAttr = await locator.getAttribute("disabled");
  const ariaDisabled = await locator.getAttribute("aria-disabled");
  const disabled = disabledAttr !== null || ariaDisabled === "true";

  // -------------------------------------------------------------------------
  // 3: arm HTTP-status watcher BEFORE the click so responses aren't missed
  // -------------------------------------------------------------------------
  const backendEvidence: BackendEvidence = {};
  let statusPromise: Promise<boolean> | undefined;
  if (opts.expectStatus !== undefined) {
    const expectedStatus = opts.expectStatus;
    backendEvidence.status_waited = expectedStatus;
    statusPromise = page
      .waitForResponse((res) => res.status() === expectedStatus, {
        timeout: 15_000,
      })
      .then(() => true)
      .catch(() => false);
  }

  // -------------------------------------------------------------------------
  // 4: perform the click
  // -------------------------------------------------------------------------
  await locator.click();

  // -------------------------------------------------------------------------
  // 5: after-click screenshot
  // -------------------------------------------------------------------------
  const tsAfter = new Date().toISOString();
  const afterFile = `after-${actionId}.png`;
  await page.screenshot({
    path: path.join(screenshotsDir, afterFile),
    fullPage: false,
  });

  // -------------------------------------------------------------------------
  // 6: resolve backend-evidence watchers
  // -------------------------------------------------------------------------

  // HTTP status
  if (statusPromise !== undefined) {
    backendEvidence.status_found = await statusPromise;
  }

  // WS frame type — poll websockets.jsonl (written by wsRecorder)
  if (opts.expectWs !== undefined) {
    backendEvidence.ws_type_waited = opts.expectWs;
    const wsFile = path.join(recorder.runDir, "websockets.jsonl");
    backendEvidence.ws_found = await pollWsType(wsFile, opts.expectWs);
  }

  // Event-log polling
  if (opts.expectEvents !== undefined && opts.expectEvents.length > 0) {
    backendEvidence.events_waited = opts.expectEvents;
    if (opts.agentBaseUrl !== undefined && opts.cid !== undefined) {
      const base = opts.agentBaseUrl.replace(/\/$/, "");
      const url = `${base}/conversations/${opts.cid}/events`;
      backendEvidence.events_found = await pollEventLog(url, opts.expectEvents);
    } else {
      // Can't poll without base + cid
      backendEvidence.events_found = false;
    }
  }

  // -------------------------------------------------------------------------
  // 7: append evidence record
  // -------------------------------------------------------------------------
  const record: UiActionRecord = {
    schema: "ui-action",
    ui_action_id: actionId,
    ts_before: tsBefore,
    ts_after: tsAfter,
    accessible_name: textContent,
    role,
    selector,
    route,
    disabled,
    screenshot_before: path.join("screenshots", beforeFile),
    screenshot_after: path.join("screenshots", afterFile),
    backend_evidence: backendEvidence,
  };
  recorder.write("ui-actions.jsonl", record);

  // -------------------------------------------------------------------------
  // 8: FAIL when requested backend evidence was not found
  //
  // Recording first (step 7) ensures the run folder preserves what we DID
  // observe, then the throw surfaces the assertion failure to the test.
  // -------------------------------------------------------------------------
  const missingEvidence: string[] = [];
  if (
    opts.expectStatus !== undefined &&
    backendEvidence.status_found === false
  ) {
    missingEvidence.push(
      `HTTP status ${opts.expectStatus} not observed in page responses`,
    );
  }
  if (opts.expectWs !== undefined && backendEvidence.ws_found === false) {
    missingEvidence.push(
      `WS frame type "${opts.expectWs}" not found in websockets.jsonl`,
    );
  }
  if (
    opts.expectEvents !== undefined &&
    opts.expectEvents.length > 0 &&
    backendEvidence.events_found === false
  ) {
    missingEvidence.push(
      `event type(s) [${opts.expectEvents.join(", ")}] not found in conversation event log`,
    );
  }
  if (missingEvidence.length > 0) {
    throw new Error(
      `discoClick [action=${actionId}]: missing backend evidence after click on "${selector}":\n` +
        missingEvidence.map((m) => `  • ${m}`).join("\n"),
    );
  }
}
