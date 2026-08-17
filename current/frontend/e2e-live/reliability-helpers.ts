import {
  expect,
  type APIRequestContext,
  type APIResponse,
} from "@playwright/test";
import { assessAgentErrorThrash } from "@/lib/harness/thrashOracle";

export const AGENT_API = (
  process.env.DISCO_RELIABILITY_AGENT_URL ?? "http://127.0.0.1:8000"
).replace(/\/$/, "");

export const APP_API = (
  process.env.DISCO_RELIABILITY_APP_URL ?? "http://127.0.0.1:8800"
).replace(/\/$/, "");

export type EventJson = {
  seq?: number;
  id?: string;
  kind?: string;
  status?: string;
  detail?: string | null;
  error?: string | null;
  source?: string;
  action_id?: string;
  // WorkspaceVersionEvent's commit sequence (`version_seq: int` in
  // _event_control.py, mirrored as a required `number` in @/types/agent). It is
  // enumerated here rather than left to the index signature below so callers get
  // `number | undefined` instead of `unknown` — build-lifecycle.spec.ts reads it
  // as the durable-commit signal.
  version_seq?: number;
  message?: { role?: string; content?: string };
  tool_call?: { tool_name?: string; arguments?: Record<string, unknown> };
  tool_result?: {
    tool_name?: string;
    call_id?: string;
    success?: boolean;
    error?: string | null;
    content?: string;
    structured?: Record<string, unknown> | null;
  };
  [key: string]: unknown;
};

export type InspectTrace = {
  event_count?: number;
  routing_decisions?: Array<Record<string, unknown>>;
  spans?: Array<{ span?: string; event?: string; [key: string]: unknown }>;
};

const csrfByRequest = new WeakMap<APIRequestContext, string>();

function reliabilityOrigin(): string {
  // APIRequestContext talks to the Agent directly. Use that request's own
  // same-host origin for pairing and CSRF mutations; coupling this helper to a
  // Vite worker port made every port beyond the service's dev allowlist fail
  // before a test began. Browser traffic still uses the single-origin Vite
  // proxy, which rewrites Origin to this same target origin.
  return new URL(AGENT_API).origin;
}

export async function getJson<T = Record<string, unknown>>(
  request: APIRequestContext,
  url: string,
  timeout = 30_000,
): Promise<T> {
  let lastError: unknown;
  for (let attempt = 0; attempt < 5; attempt += 1) {
    try {
      const response = await request.get(url, { timeout });
      if (!response.ok())
        throw new Error(
          `GET ${url} -> ${response.status()}: ${await response.text()}`,
        );
      return (await response.json()) as T;
    } catch (error) {
      lastError = error;
      await new Promise((resolve) => setTimeout(resolve, 1_000));
    }
  }
  throw lastError;
}

export async function requireReliabilityStack(
  request: APIRequestContext,
): Promise<void> {
  const health = await request.get(`${AGENT_API}/health`, { timeout: 10_000 });
  expect(health.ok(), `agent-server health failed: ${health.status()}`).toBe(
    true,
  );
  let inspect = await request.get(`${AGENT_API}/api/debug/inspect`, {
    timeout: 10_000,
  });
  if (inspect.status() === 401) {
    // Debug evidence is session-protected. Live specs run this preflight before
    // the browser's first navigation, so pair this API context explicitly rather
    // than misreporting an auth challenge as an inspect outage.
    let pairingToken =
      process.env.DISCO_RELIABILITY_PAIRING_TOKEN?.trim() ?? "";
    if (!pairingToken) {
      const pairing = await request.get(`${AGENT_API}/api/auth/pairing-token`, {
        timeout: 10_000,
      });
      if (pairing.ok()) {
        pairingToken = String((await pairing.json()).pairing_token ?? "");
      }
    }
    const minted = await request.post(`${AGENT_API}/api/auth/mint`, {
      data: pairingToken ? { pairing_token: pairingToken } : {},
      headers: { Origin: reliabilityOrigin() },
      timeout: 10_000,
    });
    expect(
      minted.ok(),
      `agent-server reliability pairing failed: ${minted.status()}`,
    ).toBe(true);
    inspect = await request.get(`${AGENT_API}/api/debug/inspect`, {
      timeout: 10_000,
    });
  }
  expect(
    inspect.ok(),
    "DISCO_INSPECT=1 is required for live reliability evidence",
  ).toBe(true);
  expect((await inspect.json()).enabled).toBe(true);
  const sessionResponse = await request.get(`${AGENT_API}/api/auth/session`, {
    timeout: 10_000,
  });
  expect(
    sessionResponse.ok(),
    `reliability session lookup failed: ${sessionResponse.status()}`,
  ).toBe(true);
  const session = (await sessionResponse.json()) as {
    authenticated?: boolean;
    csrf_token?: string;
  };
  expect(session.authenticated, "reliability request session is not authenticated").toBe(true);
  expect(session.csrf_token, "reliability request session has no CSRF token").toBeTruthy();
  csrfByRequest.set(request, session.csrf_token!);
}

export async function restartReliabilityStack(): Promise<void> {
  const control = process.env.DISCO_RELIABILITY_STACK_CONTROL_URL;
  const token = process.env.DISCO_RELIABILITY_STACK_CONTROL_TOKEN;
  expect(control, "isolated stack control URL is required").toBeTruthy();
  expect(token, "isolated stack control token is required").toBeTruthy();
  const response = await fetch(`${control}/restart`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
  });
  const body = await response.text();
  expect(
    response.ok,
    `isolated stack restart failed: ${response.status} ${body}`,
  ).toBe(true);
}

export async function authenticatedMutation(
  request: APIRequestContext,
  url: string,
  options: {
    method?: "POST" | "PUT" | "PATCH" | "DELETE";
    data?: unknown;
    timeout?: number;
  } = {},
): Promise<APIResponse> {
  let csrf = csrfByRequest.get(request);
  if (!csrf) {
    const response = await request.get(`${AGENT_API}/api/auth/session`, {
      timeout: 10_000,
    });
    if (!response.ok())
      throw new Error(`reliability session lookup failed: ${response.status()}`);
    const session = (await response.json()) as {
      authenticated?: boolean;
      csrf_token?: string;
    };
    if (!session.authenticated || !session.csrf_token)
      throw new Error("reliability mutation requires an authenticated CSRF session");
    csrf = session.csrf_token;
    csrfByRequest.set(request, csrf);
  }
  return request.fetch(url, {
    method: options.method ?? "POST",
    data: options.data,
    headers: {
      Origin: reliabilityOrigin(),
      "X-Disco-CSRF": csrf,
    },
    timeout: options.timeout,
  });
}

export async function deleteConversation(
  request: APIRequestContext,
  cid: string,
): Promise<void> {
  const response = await authenticatedMutation(
    request,
    `${AGENT_API}/conversations/${cid}`,
    {
      method: "DELETE",
      timeout: 30_000,
    },
  );
  const text = await response.text();
  expect(
    response.ok(),
    `conversation cleanup failed: ${response.status()} ${text}`,
  ).toBe(true);
  const body = text ? (JSON.parse(text) as { deleted?: boolean }) : {};
  expect(body.deleted, `conversation ${cid} was not deleted`).toBe(true);
}

export async function conversationState(
  request: APIRequestContext,
  cid: string,
): Promise<Record<string, unknown>> {
  return getJson(request, `${AGENT_API}/conversations/${cid}/state`);
}

export async function waitForStatus(
  request: APIRequestContext,
  cid: string,
  wanted: Set<string>,
  timeoutMs: number,
): Promise<string> {
  const deadline = Date.now() + timeoutMs;
  let latest = "";
  while (Date.now() < deadline) {
    const state = await conversationState(request, cid);
    latest = String(state.execution_status ?? "");
    if (wanted.has(latest)) return latest;
    if (new Set(["ERROR", "STUCK"]).has(latest) && !wanted.has(latest)) {
      throw new Error(
        `conversation ${cid} reached ${latest}: ${String(state.detail ?? "")}`,
      );
    }
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
  throw new Error(
    `conversation ${cid} stayed ${latest}; wanted ${[...wanted].join(", ")}`,
  );
}

export async function allEvents(
  request: APIRequestContext,
  cid: string,
): Promise<EventJson[]> {
  const events: EventJson[] = [];
  let after = 0;
  for (let page = 0; page < 100; page += 1) {
    const body = await getJson<{ events?: EventJson[] }>(
      request,
      `${AGENT_API}/conversations/${cid}/events?after_seq=${after}&limit=200`,
    );
    const batch = body.events ?? [];
    if (batch.length === 0) break;
    events.push(...batch);
    after = Number(batch.at(-1)?.seq ?? after);
  }
  return events;
}

export async function inspectTrace(
  request: APIRequestContext,
  cid: string,
): Promise<InspectTrace> {
  return getJson(request, `${AGENT_API}/api/debug/trace/${cid}`);
}

function stableAction(event: EventJson): string | null {
  const call = event.tool_call;
  if (event.kind !== "action" || !call?.tool_name) return null;
  return `${call.tool_name}:${JSON.stringify(call.arguments ?? {}, Object.keys(call.arguments ?? {}).sort())}`;
}

export function assertNoThrash(
  events: EventJson[],
  trace: InspectTrace,
  options: { requireCompletedAgentStep?: boolean } = {},
): void {
  expect(
    events.some((event) => event.kind === "status" && event.status === "STUCK"),
  ).toBe(false);
  const routing = trace.routing_decisions ?? [];
  const stepEnds = (trace.spans ?? []).filter(
    (span) => span.span === "agent.step" && span.event === "end",
  );
  const repairs = (trace.spans ?? []).filter(
    (span) => span.span === "agent.repair" && span.event === "point",
  );
  expect(
    routing.length,
    "inspect trace has no model routing decisions",
  ).toBeGreaterThan(0);
  if (options.requireCompletedAgentStep !== false) {
    expect(
      stepEnds.length,
      "inspect trace has no completed agent.step spans",
    ).toBeGreaterThan(0);
  }
  expect(
    repairs.length,
    `too many hidden model/provider repairs: ${JSON.stringify(repairs)}`,
  ).toBeLessThanOrEqual(3);

  const agentErrors = assessAgentErrorThrash(events);
  expect(
    agentErrors.violations,
    `standalone agent_error thrash: ${JSON.stringify(agentErrors)}`,
  ).toEqual([]);
  const repairCounts = new Map<string, number>();
  for (const repair of repairs) {
    const kind = String(repair.repair_kind ?? "unknown");
    repairCounts.set(kind, (repairCounts.get(kind) ?? 0) + 1);
    expect(
      repairCounts.get(kind),
      `same hidden repair repeated: ${kind}`,
    ).toBeLessThanOrEqual(1);
  }

  let previous: string | null = null;
  let streak = 0;
  for (const event of events) {
    const current = stableAction(event);
    if (current === null) continue;
    streak = current === previous ? streak + 1 : 1;
    previous = current;
    expect(
      streak,
      `identical tool action repeated ${streak} times: ${current}`,
    ).toBeLessThanOrEqual(2);
  }

  const actions = new Map<string, string>();
  const errorStreaks = new Map<string, number>();
  for (const event of events) {
    if (event.kind === "action" && event.id && event.tool_call?.tool_name) {
      actions.set(event.id, event.tool_call.tool_name);
    }
    if (event.kind !== "observation" || event.tool_result?.success !== false)
      continue;
    const tool = actions.get(String(event.action_id ?? "")) ?? "unknown";
    const error = String(
      event.tool_result.error ?? event.tool_result.content ?? "",
    ).trim();
    const key = `${tool}:${error}`;
    errorStreaks.set(key, (errorStreaks.get(key) ?? 0) + 1);
    expect(
      errorStreaks.get(key),
      `same tool error repeated: ${key}`,
    ).toBeLessThanOrEqual(1);
  }
}

export function conversationIdFromWebSocket(url: string): string | null {
  const match = url.match(/\/ws\/conversations\/([^/?#]+)/);
  return match ? decodeURIComponent(match[1]) : null;
}
