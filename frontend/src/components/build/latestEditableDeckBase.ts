import type { AgentEvent } from "@/types/agent";

/** Return the latest deck whose current render still has an editable sidecar. */
export function latestEditableDeckBase(events: AgentEvent[]): string | null {
  const decided = new Set<string>();
  for (let i = events.length - 1; i >= 0; i--) {
    const event = events[i];
    if (event.kind !== "observation") continue;
    const { tool_name, structured, success } = event.tool_result;
    if (tool_name !== "slides_generate" || !success || !structured) continue;
    const editable = structured.editable_source;
    const baseName = typeof structured.base_name === "string" ? structured.base_name : null;
    const base =
      baseName ??
      (typeof editable === "string" && editable
        ? editable.replace(/\.authored\.json$/i, "")
        : null);
    if (!base || decided.has(base)) continue;
    decided.add(base);
    if (base.includes("/")) continue;
    if (typeof editable === "string" && editable) return base;
  }
  return null;
}
