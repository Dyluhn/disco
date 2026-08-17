/**
 * Pure derivations extracted from E2EBridgeMounter's route→surface mapping
 * (App.tsx, gap #4). Each function's branching is exactly what it was in the
 * component — only relocated, so it's measured on its own instead of piling
 * onto E2EBridgeMounter's cyclomatic count.
 */
import type { Surface } from "@/lib/e2eBridge";
import type { Mode } from "@/shell/mode";

const CID_ROUTE_PATTERN = /^\/(build|agent|deep|imported|share)\/([^/]+)/;
const CID_ROUTE_PREFIXES = new Set(["build", "agent", "deep", "imported"]);

const PREFIX_SURFACE: Partial<Record<string, Surface>> = {
  build: "build",
  agent: "agent",
  deep: "deep_research",
  imported: "imported",
  share: "share",
};

const PATH_SURFACE: Partial<Record<string, Surface>> = {
  "/activity": "activity",
  "/history": "history",
  "/projects": "projects",
  "/workflows": "workflows",
  "/spaces": "spaces",
  "/settings": "settings",
};

/** Parse `/build|agent|deep|imported|share/<cid>` from the pathname. */
export function deriveRouteMatch(pathname: string): {
  routePrefix: string | null;
  conversationId: string | null;
} {
  const cidMatch = pathname.match(CID_ROUTE_PATTERN);
  const routePrefix = cidMatch?.[1] ?? null;
  const conversationId =
    routePrefix && CID_ROUTE_PREFIXES.has(routePrefix) ? (cidMatch?.[2] ?? null) : null;
  return { routePrefix, conversationId };
}

function deriveSurfaceFromMode(mode: Mode): Surface {
  if (mode === "build") return "build";
  if (mode === "agent") return "agent";
  return "search";
}

/** The active surface for the bridge, derived from the route + the mode slider
 * (for "/" and ""). */
export function deriveSurface(pathname: string, routePrefix: string | null, mode: Mode): Surface {
  if (routePrefix && PREFIX_SURFACE[routePrefix]) return PREFIX_SURFACE[routePrefix];
  if (pathname in PATH_SURFACE) return PATH_SURFACE[pathname] ?? null;
  if (pathname === "/" || pathname === "") return deriveSurfaceFromMode(mode);
  return null;
}
