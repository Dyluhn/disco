/**
 * What the browser tab says.
 *
 * `index.html` ships one static "Disco — research" and nothing ever changed it,
 * so every tab, bookmark and history entry read the same regardless of where the
 * user was (UI-28). The title is derived here from the route, the mode slider,
 * and — when a surface is showing one conversation — that conversation's own
 * title, which the surface publishes through the seam below.
 *
 * One owner: `DocumentTitle` in App.tsx assigns `document.title` and nothing
 * else does, so there is no ordering question between the route and the surface.
 */
import type { Mode } from "@/shell/mode";

export const APP_NAME = "Disco";

/** Long research questions make an unreadable tab; keep the identifying head. */
const MAX_TITLE_CHARS = 60;

const PATH_TITLE: Record<string, string> = {
  "/activity": "Activity",
  "/history": "History",
  "/projects": "Projects",
  "/workflows": "Workflows",
  "/spaces": "Spaces",
  "/settings": "Settings",
};

const PREFIX_TITLE: Record<string, string> = {
  build: "Build",
  agent: "Agent",
  deep: "Deep Research",
  imported: "Imported run",
  share: "Shared report",
};

const MODE_TITLE: Record<Mode, string> = {
  search: "Search",
  build: "Build",
  agent: "Agent",
};

function routeName(pathname: string, mode: Mode): string {
  const path = pathname.length > 1 ? pathname.replace(/\/+$/, "") : pathname;
  if (path in PATH_TITLE) return PATH_TITLE[path];
  const prefix = path.match(/^\/([^/]+)\//)?.[1];
  if (prefix && prefix in PREFIX_TITLE) return PREFIX_TITLE[prefix];
  return MODE_TITLE[mode];
}

function shorten(title: string): string {
  return title.length > MAX_TITLE_CHARS
    ? `${title.slice(0, MAX_TITLE_CHARS - 1).trimEnd()}…`
    : title;
}

/** The tab title for this route: the open conversation's own name when a
 *  surface published one, otherwise where the user is. */
export function documentTitle(
  pathname: string,
  mode: Mode,
  conversationTitle: string | null,
): string {
  const named = conversationTitle?.trim();
  return `${shorten(named || routeName(pathname, mode))} — ${APP_NAME}`;
}

let published: string | null = null;
const listeners = new Set<() => void>();

/** Called by the surface showing a single conversation, with its title (and
 *  `null` on unmount). */
export function publishConversationTitle(title: string | null): void {
  const next = title?.trim() || null;
  if (next === published) return;
  published = next;
  for (const listener of listeners) listener();
}

export function subscribeConversationTitle(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function getConversationTitle(): string | null {
  return published;
}
