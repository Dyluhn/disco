/**
 * Backend-availability predicates for components and views (Amendment A3).
 *
 * "Is a backend configured?" is a question about the transport, so its answer
 * is owned by the api layer. Components used to import `isLive`/`agentLive`
 * straight from `./client` to decide whether to disable a probe button or fall
 * back to fixture data; that put the ONE transport module in the import graph
 * of the view layer for a boolean.
 *
 * Declared wrappers rather than re-exports, for the same reason as `./errors`:
 * the public-API authority records a re-export as a re-export, not a
 * declaration. Semantics are byte-faithful to the client predicates they wrap.
 */

import { agentLive, isLive } from "./client";

/** True when the app-server base URL is configured — the api modules run live. */
export function apiIsLive(): boolean {
  return isLive();
}

/** True when the agent-server is configured — the Build surface runs live, and
 *  live probes ("Test image", "Test voice") can actually reach something. */
export function agentIsLive(): boolean {
  return agentLive();
}
