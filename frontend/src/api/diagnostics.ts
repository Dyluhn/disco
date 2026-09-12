/**
 * Diagnostics: the in-process half of the doctor (agent-server) and the app-server's
 * health body. Live → real fetch through the client; demo → a plausible fixture so the
 * Settings section renders in the design-review build.
 */
import type { AppServerHealth, Diagnostics } from "@/types/diagnostics";
import { agentGet, agentLive, apiGet, fixtureDelay, isLive } from "./client";

const FIXTURE_BUILD = { tag: "v0.2.0", commit: "3f9c1a2", source: "image" } as const;

const FIXTURE_DIAGNOSTICS: Diagnostics = {
  generated_at: "2026-09-12T00:00:00Z",
  version: "v0.2.0 (3f9c1a2)",
  build: FIXTURE_BUILD,
  agent_server: {
    status: "ok",
    version: "v0.2.0 (3f9c1a2)",
    build: FIXTURE_BUILD,
    checks: { store: "ok", runtime: "ok", models: 3 },
  },
  sandbox: { reachable: true, backend: "podman", detail: "ok" },
  checks: [
    { name: "config", status: "PASS", detail: "build v0.2.0 (3f9c1a2) (image); 14 DISCO_* variables set" },
    { name: "data disk", status: "PASS", detail: "41.2 GB free at /data" },
    { name: "database", status: "PASS", detail: "/data/disco.db: quick_check ok, 18342 events, 61 MB" },
    { name: "secret key", status: "PASS", detail: "DISCO_SECRET_KEY set" },
  ],
  env: { DISCO_LOG_LEVEL: "INFO", DISCO_SECRET_KEY: "***REDACTED***" },
};

const FIXTURE_APP: AppServerHealth = {
  status: "ok",
  service: "app-server",
  version: "v0.2.0 (3f9c1a2)",
  build: FIXTURE_BUILD,
};

export async function getDiagnostics(): Promise<Diagnostics> {
  if (agentLive()) return agentGet<Diagnostics>("/api/diagnostics");
  await fixtureDelay();
  return FIXTURE_DIAGNOSTICS;
}

export async function getAppServerHealth(): Promise<AppServerHealth> {
  if (isLive()) return apiGet<AppServerHealth>("/api/health");
  await fixtureDelay();
  return FIXTURE_APP;
}
