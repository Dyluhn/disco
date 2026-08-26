import { agentLive, agentSend } from "./client";
import { FIXTURE_CID } from "@/fixtures/agentTrace";

export interface ReportDeckStart {
  ok: boolean;
  conversation_id: string;
  job_id: string;
  source_event_id: string;
  contract: "deck";
  format: "pptx";
}

/** Start the server-owned typed report → deck job.
 *
 * The client sends only the source conversation id. The server resolves the
 * owner-visible latest ReportEvent and supplies it to the structured deck
 * pipeline; report prose never becomes client-authored authority. */
export async function startReportDeck(conversationId: string): Promise<ReportDeckStart> {
  if (!agentLive()) {
    return {
      ok: true,
      conversation_id: FIXTURE_CID,
      job_id: FIXTURE_CID,
      source_event_id: "evt_fixture_report",
      contract: "deck",
      format: "pptx",
    };
  }
  return agentSend<ReportDeckStart>(
    "POST",
    `/api/conversations/${encodeURIComponent(conversationId)}/report/deck`,
  );
}
