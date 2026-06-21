/**
 * artifactCollector — fetch app truth endpoints after a meaningful action.
 *
 * Calls the agent-server's per-conversation endpoints and saves the responses
 * under  conversations/<cid>/  in the run folder.  All 404s are tolerated
 * silently (e.g. /api/debug/trace/{cid} is inert without DISCO_INSPECT=1).
 * Network errors are recorded to artifact-collector-errors.jsonl and skipped.
 *
 * evidence-harness-campaign.md W11
 */

import * as fs from "node:fs";
import * as path from "node:path";
import type { Recorder } from "./recorder";
import { redact } from "./schema";

// ---------------------------------------------------------------------------
// Endpoint table
// ---------------------------------------------------------------------------

interface Endpoint {
  /** Filename to write under conversations/<cid>/. */
  file: string;
  /** URL path.  Occurrences of {cid} are replaced before fetching. */
  urlPath: string;
}

/**
 * Truth endpoints called after each meaningful interaction.
 * Mirrors the list in evidence-harness-campaign.md §"Three-layer harness".
 */
const ENDPOINTS: Endpoint[] = [
  { file: "events.json", urlPath: "/conversations/{cid}/events" },
  { file: "state.final.json", urlPath: "/conversations/{cid}/state" },
  { file: "inspect-trace.json", urlPath: "/api/debug/trace/{cid}" },
  { file: "workspace-manifest.json", urlPath: "/api/projects/{cid}/manifest" },
];

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Fetch all truth endpoints for {@link cid} from {@link agentBaseUrl} and
 * write each successful response to  conversations/<cid>/  in the run folder.
 *
 * @param cid           Conversation / request ID to query.
 * @param agentBaseUrl  Root URL of the agent-server, e.g. "http://127.0.0.1:8000".
 * @param recorder      Active Recorder for this run.
 */
export async function collectArtifacts(
  cid: string,
  agentBaseUrl: string,
  recorder: Recorder,
): Promise<void> {
  recorder.noteConversation(cid);
  const cidDir = path.join(recorder.runDir, "conversations", cid);

  for (const ep of ENDPOINTS) {
    const urlPath = ep.urlPath.replace("{cid}", cid);
    const url = `${agentBaseUrl.replace(/\/$/, "")}${urlPath}`;

    try {
      const res = await fetch(url);
      if (!res.ok) {
        // 404 expected when endpoint is gated or conversation doesn't exist yet.
        if (res.status !== 404) {
          recorder.write("artifact-collector-errors.jsonl", {
            cid,
            url,
            status: res.status,
            ts: new Date().toISOString(),
          });
        }
        continue;
      }
      const text = await res.text();
      // Redact secrets before writing to disk.  Parse JSON so the key-based
      // redaction pass also runs; fall back to string-content scrubbing for
      // non-JSON bodies (plain text, HTML error pages, etc.).
      let safeContent: string;
      try {
        const parsed: unknown = JSON.parse(text);
        safeContent = JSON.stringify(redact(parsed), null, 2) + "\n";
      } catch {
        safeContent = redact(text) as string;
      }
      fs.writeFileSync(path.join(cidDir, ep.file), safeContent, "utf-8");
    } catch (err: unknown) {
      // Network errors (ECONNREFUSED, timeout, etc.) — record and skip.
      recorder.write("artifact-collector-errors.jsonl", {
        cid,
        url,
        error: err instanceof Error ? err.message : String(err),
        ts: new Date().toISOString(),
      });
    }
  }
}
