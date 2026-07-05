import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState, type ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as clientModule from "@/api/client";
import { SourcePicker } from "./SourcePicker";

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: "",
    headers: { get: () => null },
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

function Harness() {
  const [selected, setSelected] = useState<string[]>(["ddgs"]);
  return (
    <>
      <SourcePicker selected={selected} onChange={setSelected} />
      <output data-testid="selected-sources">{selected.join(",")}</output>
    </>
  );
}

function wrapper() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return ({ children }: { children: ReactElement }) => (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.spyOn(clientModule, "isLive").mockReturnValue(true);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/data-sources/config") {
        return jsonResponse({
          search_provider: "ddgs",
          search_base_url: "",
          search_api_key_env: "",
          extraction_provider: "local",
          extraction_base_url: "",
          extraction_api_key_env: "",
          configured_sources: ["tavily"],
        });
      }
      throw new Error(`unexpected fetch: ${url}`);
    }),
  );
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("SourcePicker", () => {
  it("toggles keyless/configured sources and disables unconfigured keyed sources", async () => {
    const user = userEvent.setup();
    render(<Harness />, { wrapper: wrapper() });

    const web = screen.getByRole("button", { name: /web/i });
    const arxiv = screen.getByRole("button", { name: /arxiv/i });
    const tavily = await screen.findByRole("button", { name: /tavily/i });
    const brave = screen.getByRole("button", { name: /brave/i });

    expect(web).toHaveAttribute("aria-pressed", "true");
    await waitFor(() => expect(tavily).toBeEnabled());
    expect(brave).toBeDisabled();
    expect(screen.getAllByRole("link", { name: /add in settings/i }).length).toBeGreaterThan(0);

    await user.click(arxiv);
    expect(screen.getByTestId("selected-sources")).toHaveTextContent("ddgs,arxiv");

    await user.click(tavily);
    expect(screen.getByTestId("selected-sources")).toHaveTextContent("ddgs,arxiv,tavily");

    await user.click(web);
    expect(screen.getByTestId("selected-sources")).toHaveTextContent("arxiv,tavily");

    await user.click(brave);
    await waitFor(() =>
      expect(screen.getByTestId("selected-sources")).toHaveTextContent("arxiv,tavily"),
    );
  });
});
