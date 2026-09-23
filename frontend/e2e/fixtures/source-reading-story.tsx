import { createRoot } from "react-dom/client";
import "@/styles/theme.css";
import { DeepProgressStrip } from "@/components/research/DeepProgressStrip";
import { deriveActivity, deriveLiveTrace, deriveStats } from "@/lib/deepResearchTrace";
import type { ActionEvent } from "@/types/agent";
import captured from "@/lib/__fixtures__/source-reading-activity.json";

function Panel({ mode }: { mode: "waiting" | "generating" }) {
  const row = captured[mode];
  const events: ActionEvent[] = [{ id: mode, kind: "action", seq: 1,
    timestamp: new Date().toISOString(), thought: "",
    tool_call: { tool_name: row.kind, arguments: row.payload } }];
  return <section data-reading={mode} style={{ margin: "32px 0" }}>
    <h2>{mode === "waiting" ? "Request started" : "Source output arriving"}</h2>
    <DeepProgressStrip brief={null} trace={deriveLiveTrace(events, "RUNNING")}
      stats={deriveStats(events)} status="RUNNING" activity={deriveActivity(events)}
      followUpStatus={null} />
  </section>;
}

createRoot(document.getElementById("root")!).render(
  <main style={{ maxWidth: 1000, margin: "40px auto", padding: 24 }}>
    <h1>Source reading · captured provider activity</h1>
    <Panel mode="waiting" /><Panel mode="generating" />
  </main>,
);
