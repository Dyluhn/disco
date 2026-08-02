/**
 * Miscellaneous platform toggles: the live-browser (noVNC) toggle and the
 * vestigial build-kernel config.
 *
 * Extracted from api/models.ts (module-size decomposition, TS-0003);
 * re-exported (as thin delegators) from api/models so the public import path
 * and every signature stay unchanged.
 */

import type { BuildKernelConfig, LiveBrowserConfig } from "@/types/models";
import { apiGet, apiSend, fixtureDelay, isLive } from "../client";

// ---- live browser (noVNC toggle) -------------------------------------------

let fixtureLiveBrowser: LiveBrowserConfig = { enabled: false };

export async function getLiveBrowserConfig(): Promise<LiveBrowserConfig> {
  if (isLive()) return apiGet<LiveBrowserConfig>("/api/live-browser/config");
  await fixtureDelay();
  return { ...fixtureLiveBrowser };
}

export async function updateLiveBrowserConfig(cfg: LiveBrowserConfig): Promise<LiveBrowserConfig> {
  if (isLive()) return apiSend<LiveBrowserConfig>("PUT", "/api/live-browser/config", cfg);
  await fixtureDelay();
  fixtureLiveBrowser = { ...cfg };
  return { ...fixtureLiveBrowser };
}

// ---- vestigial build kernel config -----------------------------------------

let fixtureBuildKernel: BuildKernelConfig = { kind: "disco" };

export async function getBuildKernelConfig(): Promise<BuildKernelConfig> {
  if (isLive()) return apiGet<BuildKernelConfig>("/api/build-kernel/config");
  await fixtureDelay();
  return { ...fixtureBuildKernel };
}

export async function updateBuildKernelConfig(
  cfg: Pick<BuildKernelConfig, "kind">,
): Promise<BuildKernelConfig> {
  if (isLive()) return apiSend<BuildKernelConfig>("PUT", "/api/build-kernel/config", cfg);
  await fixtureDelay();
  fixtureBuildKernel = { ...fixtureBuildKernel, kind: cfg.kind };
  return { ...fixtureBuildKernel };
}
