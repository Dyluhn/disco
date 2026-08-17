/**
 * Conversation schedules (RP-08) — recurring cron-style re-runs of a
 * conversation. Relocated out of ScheduleSection.tsx (Amendment A3):
 * components/ must not talk to `client.ts` directly, so these are now
 * declared here, byte-faithful to the transport the component used to own
 * (same URLs, methods, headers, bodies, error handling).
 */

import { agentFetch } from "./client";
import type { PreviewResult, ScheduleRow } from "@/components/settings/scheduleSectionParts/types";

export async function listSchedules(conversationId: string): Promise<ScheduleRow[]> {
  const r = await agentFetch(`/api/conversations/${encodeURIComponent(conversationId)}/schedules`);
  if (!r.ok) throw new Error(`Failed to load schedules: ${r.status}`);
  const body = await r.json();
  return body.schedules ?? [];
}

export async function createSchedule(
  conversationId: string,
  payload: {
    rrule: string;
    description: string;
    timezone: string;
    depth?: string;
    model_override?: string;
  },
): Promise<ScheduleRow> {
  const r = await agentFetch(`/api/conversations/${encodeURIComponent(conversationId)}/schedules`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err?.detail?.reason ?? `Error ${r.status}`);
  }
  return r.json();
}

export async function deleteSchedule(scheduleId: string): Promise<void> {
  const r = await agentFetch(`/api/schedules/${encodeURIComponent(scheduleId)}`, {
    method: "DELETE",
  });
  if (!r.ok) throw new Error(`Delete failed: ${r.status}`);
}

export async function previewSchedule(
  rrule: string,
  timezone: string,
  n = 3,
): Promise<PreviewResult> {
  const r = await agentFetch(`/api/schedules/preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rrule, timezone, n }),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err?.detail?.reason ?? `Error ${r.status}`);
  }
  return r.json();
}
