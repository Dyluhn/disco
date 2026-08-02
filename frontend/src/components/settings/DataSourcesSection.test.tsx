/**
 * DataSourcesSection — WALK-05 (E4): the bundled search option must label itself
 * "Bundled — ddgs" (not "Bundled — DuckDuckGo", which names a trademark).
 *
 * Drives the REAL data path: DataSourcesSection → useDataSourcesConfig →
 * @/api/... → fetch(...). Only the network boundary is stubbed (vi.stubGlobal
 * "fetch") and isLive is intercepted, matching the ProviderKeysSection.test.tsx /
 * ProjectStorageSection.test.tsx pattern. NO vi.mock of the data layer.
 *
 * Amendment A3: components/ and views/ may not import `@/api/client`.
 * `vi.mock` intercepts by specifier and needs no static import, so this
 * controls exactly the same `isLive` the previous `vi.spyOn(clientModule, …)`
 * did (DataSourcesSection now calls `apiIsLive` from `@/api/liveness`, which
 * itself wraps this same `isLive`).
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DataSourcesSection } from "./DataSourcesSection";

const { isLiveMock } = vi.hoisted(() => ({ isLiveMock: vi.fn(() => true) }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  isLive: () => isLiveMock(),
}));

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
  isLiveMock.mockReturnValue(true);
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
    expect(
      await screen.findByRole("button", {
        name: /news.*recent news headlines via google news/i,
      }),
    ).toBeInTheDocument();
    // The old trademark name must not appear
    expect(screen.queryByText(/DuckDuckGo/)).not.toBeInTheDocument();
  });
});
