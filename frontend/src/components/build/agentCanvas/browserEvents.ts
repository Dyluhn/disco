import type { LiveSignal } from "@/lib/buildTrace";
import type { AgentEvent } from "@/types/agent";

/** All screenshot_paths the browser/MCP tools produced, in chronological order
 * (latest last). Real, straight from each observation's structured payload. */
export function collectScreenshots(events: AgentEvent[]): string[] {
  const out: string[] = [];
  for (const e of events) {
    if (e.kind !== "observation") continue;
    const sp = e.tool_result.structured?.screenshot_path;
    if (typeof sp === "string" && sp) out.push(sp);
  }
  return out;
}

/** The URL of the most recent `browser` action (its arguments.url) — the page the
 * agent last drove to. Mirrors buildTrace's detailFor() browser extraction. */
export function latestBrowserUrl(events: AgentEvent[]): string | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.kind === "action" && e.tool_call?.tool_name === "browser") {
      const u = e.tool_call.arguments.url;
      if (typeof u === "string" && u) return u;
    }
  }
  return null;
}

/** The screenshot to show large: the user's manual pick if it's still present in
 * the reel, else the most recent shot. */
export function pickHeroScreenshot(picked: string | null, shots: string[]): string | null {
  if (picked && shots.includes(picked)) return picked;
  return shots[shots.length - 1] ?? null;
}

/** True while the agent is actively driving a browser (the built-in `browser`
 * tool, or an external browser-MCP tool) — independent of whether a live stream
 * is up. Drives the "driving…" pulse. */
export function isDrivingBrowser(live: LiveSignal): boolean {
  return (
    live.kind === "tool_executing" &&
    (live.tool_name === "browser" || live.tool_name.startsWith("mcp__"))
  );
}
