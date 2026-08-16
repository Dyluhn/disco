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
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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

let savedConfig: unknown = null;

function installFetch() {
  const stub = vi.fn(async (url: string, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    if (method === "GET" && url === "/api/data-sources/config") {
      return jsonResponse({
        search_provider: "ddgs",
        search_base_url: "",
        search_api_key_env: "",
        extraction_provider: "local",
        extraction_base_url: "",
        extraction_api_key_env: "",
      });
    }
    if (method === "GET" && url === "/api/secrets") {
      return jsonResponse({
        names: ["BRAVE_SEARCH_API_KEY"],
        locked_names: [],
        locked: false,
        can_store: true,
      });
    }
    if (method === "PUT" && url === "/api/data-sources/config") {
      savedConfig = JSON.parse(String(init?.body));
      return jsonResponse(savedConfig);
    }
    throw new Error(`unexpected fetch: ${method} ${url}`);
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
  savedConfig = null;
  isLiveMock.mockReturnValue(true);
  installFetch();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("DataSourcesSection — WALK-05 label", () => {
  it("shows compact defaults and reveals the complete provider choices on request", async () => {
    render(createElement(DataSourcesSection), { wrapper: makeWrapper() });

    expect(await screen.findByText(/Current: Bundled — ddgs/i)).toBeInTheDocument();
    const configure = screen.getByText("Configure search");
    expect(configure.closest("details")).not.toHaveAttribute("open");
    fireEvent.click(configure);
    expect(configure.closest("details")).toHaveAttribute("open");
    expect(await screen.findByRole("button", { name: /bundled.*ddgs/i })).toBeInTheDocument();
    expect(
      screen.getByRole("button", {
        name: /news.*recent news headlines via google news/i,
      }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Brave Search/i })).toBeInTheDocument();
    // The old trademark name must not appear
    expect(screen.queryByText(/DuckDuckGo/)).not.toBeInTheDocument();
  });

  it("persists Brave with a selected stored credential name", async () => {
    render(createElement(DataSourcesSection), { wrapper: makeWrapper() });

    fireEvent.click(await screen.findByText("Configure search"));
    fireEvent.click(screen.getByRole("button", { name: /Brave Search/i }));

    const credential = await screen.findByLabelText("Search credential");
    const listId = credential.getAttribute("list");
    expect(listId).toBeTruthy();
    await waitFor(() =>
      expect(
        document.querySelector(
          `#${CSS.escape(listId!)} option[value="BRAVE_SEARCH_API_KEY"]`,
        ),
      ).toBeInTheDocument(),
    );
    fireEvent.change(credential, { target: { value: "BRAVE_SEARCH_API_KEY" } });
    fireEvent.click(screen.getByRole("button", { name: /Save data sources/i }));

    await waitFor(() =>
      expect(savedConfig).toMatchObject({
        search_provider: "brave",
        search_api_key_env: "BRAVE_SEARCH_API_KEY",
      }),
    );
  });
});
