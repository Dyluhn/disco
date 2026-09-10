/**
 * The Agent surface inspector (right pane) — a LIGHTER sibling of ExecutionCanvas.
 * The Agent surface operates on the WORLD (browser, tools, artifacts) rather than
 * building source, so this canvas foregrounds the BROWSER and ARTIFACTS and demotes
 * the console. Everything here is REAL: derived from the event stream (no fetches
 * except the img/iframe/anchor URLs the browser loads directly). Honest by
 * construction — no faked viewport for browser-MCP servers (which return fenced
 * text, not screenshots), no broken <img> without a conversation to fetch against.
 *
 * The tab panes themselves live under `agentCanvas/` (one file per pane, plus the
 * live-browser session hook and its small render helpers); this module composes
 * them and owns the tab-selection behavior.
 */

import { useMemo, useRef, useState, useEffect } from "react";
import * as Tabs from "@radix-ui/react-tabs";
import { Globe, History, Package, Presentation, SquareTerminal } from "lucide-react";
import { cn } from "@/lib/cn";
import { deriveFiles } from "@/lib/buildTrace";
import { DeckEditorPane } from "@/components/build/DeckEditorPane";
import { latestEditableDeckBase } from "@/components/build/latestEditableDeckBase";
import type { AgentEvent, ConversationStatus } from "@/types/agent";
import { collectScreenshots } from "./agentCanvas/browserEvents";
import { BrowserPane } from "./agentCanvas/BrowserPane";
import { ArtifactsPane } from "./agentCanvas/ArtifactsPane";
import { ConsolePane } from "./agentCanvas/ConsolePane";
import { HistoryPane } from "./agentCanvas/HistoryPane";

type TabId = "browser" | "artifacts" | "console" | "history" | "deck";

const BASE_TABS: { id: TabId; label: string; icon: typeof Globe }[] = [
  { id: "browser", label: "Browser", icon: Globe },
  { id: "artifacts", label: "Artifacts", icon: Package },
  { id: "console", label: "Console", icon: SquareTerminal },
  { id: "history", label: "Agent History", icon: History },
];

export function AgentCanvas({
  events,
  status,
  cid,
  untrusted = false,
  onSteer,
}: {
  events: AgentEvent[];
  status: ConversationStatus;
  cid: string | null;
  /** Third-party events (shared/imported run) → harden the artifact preview iframe. */
  untrusted?: boolean;
  /** W-26 — steer the agent from the artifact-preview "Discuss with agent" action.
   * Undefined when steering isn't available (no false affordance). */
  onSteer?: (text: string) => void;
}) {
  // A screenshot to show → Browser is the hero; else artifacts exist → Artifacts;
  // else Browser (empty state). Initial only — don't yank the user's tab as the
  // stream grows (mirrors ExecutionCanvas's useMemo([]) pattern).
  const initial: TabId = useMemo(
    () =>
      collectScreenshots(events).length > 0
        ? "browser"
        : deriveFiles(events).length > 0
          ? "artifacts"
          : "browser",
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );
  const [tab, setTab] = useState<TabId>(initial);

  const hasScreenshots = collectScreenshots(events).length > 0;
  useEffect(() => {
    if (hasScreenshots) setTab("browser");
  }, [hasScreenshots]);

  // A2: the Edit/Export Slides tab appears ONLY when a deck with an editable AuthoredDeck
  // sidecar exists AND there's a conversation to edit against (no false affordance).
  const editableDeckBase = useMemo(() => latestEditableDeckBase(events), [events]);

  // BW-15: when a fresh slides_generate just completed, auto-foreground the
  // Edit/Export Slides tab (mirrors the hasScreenshots→Browser auto-switch). Fire
  // ONLY on the absent→present transition of editableDeckBase — a ref-remembered
  // prev base keeps it from re-firing on every re-render (so it doesn't fight a
  // later manual tab click). Declared AFTER the hasScreenshots effect so, in a
  // commit where both transition, the deck switch wins for a slides build.
  const prevDeckBaseRef = useRef<string | null>(null);
  useEffect(() => {
    const prev = prevDeckBaseRef.current;
    prevDeckBaseRef.current = editableDeckBase;
    if (!prev && editableDeckBase && cid) setTab("deck");
  }, [editableDeckBase, cid]);

  const tabs = useMemo(
    () =>
      editableDeckBase && cid
        ? [...BASE_TABS, { id: "deck" as TabId, label: "Edit/Export Slides", icon: Presentation }]
        : BASE_TABS,
    [editableDeckBase, cid],
  );

  // If the editable deck disappears (e.g. events pruned) while its tab is active,
  // fall back to Artifacts so we never sit on a dead tab.
  useEffect(() => {
    if (tab === "deck" && !(editableDeckBase && cid)) setTab("artifacts");
  }, [tab, editableDeckBase, cid]);

  return (
    <Tabs.Root
      value={tab}
      onValueChange={(v) => setTab(v as TabId)}
      className="flex h-full min-h-0 flex-col"
    >
      {/* overflow-x-auto: the tab set grows to 5 with the Edit/Export Slides tab
          (deck builds) — on a phone-width viewport that's wider than the
          screen, so it becomes ONE deliberate horizontal scroller rather than
          wrapping or clipping (mirrors ExecutionCanvas's identical fix). */}
      <Tabs.List className="flex shrink-0 items-center gap-px overflow-x-auto border-b border-hairline px-inline">
        {tabs.map((t) => (
          <Tabs.Trigger
            key={t.id}
            value={t.id}
            className={cn(
              "flex max-lg:min-h-11 shrink-0 items-center gap-hair px-inline py-inline font-ui text-[0.78rem] text-text-muted transition-colors",
              "border-b-2 border-transparent hover:text-text",
              "data-[state=active]:border-accent data-[state=active]:text-text",
            )}
          >
            <t.icon className="size-3.5" aria-hidden />
            {t.label}
          </Tabs.Trigger>
        ))}
      </Tabs.List>
      <div className="min-h-0 flex-1">
        <Tabs.Content value="browser" className="h-full focus:outline-none">
          <BrowserPane events={events} status={status} cid={cid} />
        </Tabs.Content>
        <Tabs.Content value="artifacts" className="h-full focus:outline-none">
          <ArtifactsPane
            events={events}
            status={status}
            cid={cid}
            untrusted={untrusted}
            onSteer={onSteer}
          />
        </Tabs.Content>
        <Tabs.Content value="console" className="h-full focus:outline-none">
          <ConsolePane events={events} />
        </Tabs.Content>
        <Tabs.Content value="history" className="h-full focus:outline-none">
          <HistoryPane events={events} status={status} cid={cid} />
        </Tabs.Content>
        {editableDeckBase && cid && (
          <Tabs.Content value="deck" className="h-full focus:outline-none">
            <DeckEditorPane cid={cid} base={editableDeckBase} />
          </Tabs.Content>
        )}
      </div>
    </Tabs.Root>
  );
}
