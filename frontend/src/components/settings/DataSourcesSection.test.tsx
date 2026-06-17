/**
 * DataSourcesSection — WALK-05 (E4): the bundled search option must label itself
 * "Bundled — ddgs" (not "Bundled — DuckDuckGo", which names a trademark).
 *
 * Drives the REAL data path: DataSourcesSection → useDataSourcesConfig →
 * @/api/... → fetch(...). Only the network boundary is stubbed (vi.stubGlobal
 * "fetch") and isLive is spied on, matching the ProviderKeysSection.test.tsx /
 * ProjectStorageSection.test.tsx pattern. NO vi.mock of the data layer.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as clientModule from "@/api/client";
import { DataSourcesSection } from "./DataSourcesSection";

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: "",
    headers: { get: () => null },
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

function installFetch() {
  const stub = vi.fn(async (url: string) => {
    if (url === "/api/data-sources/config") {
      return jsonResponse({
        search_provider: "ddgs",
        search_base_url: "",
        search_api_key_env: "",
        extraction_provider: "local",
        extraction_base_url: "",
        extraction_api_key_env: "",
      });
    }
    throw new Error(`unexpected fetch: ${url}`);
  });
  vi.stubGlobal("fetch", stub);
  return stub;
}

function makeWrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return ({ children }: { children: React.ReactNode }) =>
    createElement(QueryClientProvider, { client: qc }, children);
}

beforeEach(() => {
  vi.spyOn(clientModule, "isLive").mockReturnValue(true);
  installFetch();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("DataSourcesSection — WALK-05 label", () => {
  it("WALK-05: shows 'Bundled — ddgs' (not 'Bundled — DuckDuckGo') for the bundled search option", async () => {
    render(createElement(DataSourcesSection), { wrapper: makeWrapper() });

    // Wait for the config to load and the options to render
    const btn = await screen.findByRole("button", { name: /bundled.*ddgs/i });
    expect(btn).toBeInTheDocument();
    // The old trademark name must not appear
    expect(screen.queryByText(/DuckDuckGo/)).not.toBeInTheDocument();
  });
});
