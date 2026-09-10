/**
 * Recorder — durable evidence writer for one e2e-full run.
 *
 * Opens a run folder at  test-record/e2e-full/<run-id>/  (relative to CWD,
 * which is the frontend/ directory when Playwright is invoked normally).
 *
 * evidence-harness-campaign.md W11
 */

import * as fs from "node:fs";
import * as path from "node:path";
import { SCHEMA_VERSION, redact } from "./schema";

/** Root for all evidence runs. Relative to the test-runner CWD (frontend/). */
const RECORD_ROOT = path.resolve(process.cwd(), "test-record", "e2e-full");

interface TimelineEntry {
  ts: string;
  file: string;
  data: unknown;
}

export class Recorder {
  /** Unique identifier for this run, safe for use as a directory name. */
  readonly runId: string;
  /** Absolute path to the run folder on disk. */
  readonly runDir: string;

  private readonly _conversations = new Set<string>();
  private readonly _timeline: TimelineEntry[] = [];
  private _dossierWritten = false;

  constructor(runId?: string) {
    this.runId =
      runId ??
      `run-${new Date()
        .toISOString()
        .replace(/[^0-9T]/g, "-")
        .replace(/-{2,}/g, "-")
        .slice(0, 18)}`;
    this.runDir = path.join(RECORD_ROOT, this.runId);
    fs.mkdirSync(this.runDir, { recursive: true });
  }

  /**
   * Append one JSON line (secrets redacted) to {@link file} inside the run folder.
   * Creates the file on first call.
   */
  write(file: string, obj: unknown): void {
    const safe = redact(obj);
    fs.appendFileSync(
      path.join(this.runDir, file),
      JSON.stringify(safe) + "\n",
      "utf-8",
    );
    this._timeline.push({ ts: new Date().toISOString(), file, data: safe });
  }

  /**
   * Record a discovered conversation_id and create its evidence sub-folder.
   * Safe to call multiple times with the same cid.
   */
  noteConversation(cid: string): void {
    if (this._conversations.has(cid)) return;
    this._conversations.add(cid);
    fs.mkdirSync(path.join(this.runDir, "conversations", cid), {
      recursive: true,
    });
  }

  /**
   * Write  timeline.md  +  index.html  +  manifest.json  into the run folder.
   * Safe to call multiple times; each call overwrites with the current timeline.
   */
  dossier(): void {
    this._dossierWritten = true;

    const manifest = {
      schema_version: SCHEMA_VERSION,
      run_id: this.runId,
      created_at: new Date().toISOString(),
      conversations: [...this._conversations],
      entry_count: this._timeline.length,
    };
    fs.writeFileSync(
      path.join(this.runDir, "manifest.json"),
      JSON.stringify(manifest, null, 2) + "\n",
      "utf-8",
    );

    const md = this._renderTimeline();
    fs.writeFileSync(path.join(this.runDir, "timeline.md"), md, "utf-8");
    fs.writeFileSync(
      path.join(this.runDir, "index.html"),
      this._renderHtml(md),
      "utf-8",
    );
  }

  /** Whether {@link dossier} has been called at least once. */
  get isDossierWritten(): boolean {
    return this._dossierWritten;
  }

  // ---------------------------------------------------------------------------
  // Private rendering helpers
  // ---------------------------------------------------------------------------

  private _renderTimeline(): string {
    const convList = [...this._conversations].join(", ") || "(none)";
    const lines: string[] = [
      `# Evidence Run: ${this.runId}`,
      "",
      `Created: ${new Date().toISOString()}`,
      `Conversations: ${convList}`,
      "",
      "## Timeline",
      "",
    ];
    for (const entry of this._timeline) {
      lines.push(`### ${entry.ts} — ${entry.file}`, "", "```json");
      lines.push(JSON.stringify(entry.data, null, 2));
      lines.push("```", "");
    }
    return lines.join("\n");
  }

  private _renderHtml(md: string): string {
    const esc = (s: string): string =>
      s
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Evidence Run: ${this.runId}</title>
<style>
body { font-family: system-ui, sans-serif; max-width: 960px; margin: 2rem auto; padding: 0 1rem; }
pre { background: #f4f4f4; padding: 1rem; overflow-x: auto; border-radius: 4px; white-space: pre-wrap; }
h1, h2, h3 { font-weight: 600; }
h3 { color: #555; font-size: 0.9rem; }
</style>
</head>
<body>
<pre>${esc(md)}</pre>
</body>
</html>
`;
  }
}
