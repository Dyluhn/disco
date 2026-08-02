/**
 * ProjectStorageSection live test — e4ux.
 *
 * Asserts that the Settings → Project Storage section surfaces the SERVER-RESOLVED
 * default path (auto-created when projects_root is "") with a "(default)" marker
 * when the user hasn't set one — so a fresh install can see WHERE builds will
 * save, not just an empty input. The DTO carries the real directory on
 * `effective_root`; the section renders it (not the empty input) as the active
 * location while leaving the input empty so the user can override.
 *
 * Drives the REAL data path: ProjectStorageSection → useProjectsConfig →
 * @/api/projects → @/api/client → fetch(...). The network boundary is the
 * ONLY thing stubbed (vi.stubGlobal("fetch")), same seam as McpSection.live.test.tsx.
 *
 * NO vi.mock("@/api/projects") — module-mocking the data layer would let the
 * test pass even if the fetch wiring or the /api/projects/storage/config
 * contract were broken (the vacuous-test REJECT the test brief calls out).
 *
 * Amendment A3: components/ and views/ may not import `@/api/client`. `vi.mock`
 * intercepts by specifier and needs no static import, so this controls exactly
 * the same `isLive` the previous `vi.spyOn(clientModule, …)` did.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProjectStorageSection } from "./ProjectStorageSection";
import type { ProjectStorageConfig } from "@/types/project";

const { isLiveMock } = vi.hoisted(() => ({ isLiveMock: vi.fn(() => true) }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  isLive: () => isLiveMock(),
}));

// ---- the contract the server emits (proven by test_app.py) ------------------

/** Default-on-install: projects_root is "" and the server returns the
 *  auto-created, auto-resolved path on `effective_root` (e4ux). */
function defaultServerConfig(): ProjectStorageConfig {
  return {
    projects_root: "",
    status: "ok",
    effective_root: "/home/you/.local/share/disco/projects",
  };
}

/** User-set: projects_root is the chosen path and `effective_root` mirrors it. */
function explicitServerConfig(path: string): ProjectStorageConfig {
  return {
    projects_root: path,
    status: "ok",
    effective_root: path,
  };
}

// ---- a stateful fetch router (the "server") --------------------------------

let serverState: ProjectStorageConfig;

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
  const stub = vi.fn(async (url: string, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    if (method === "GET" && url === "/api/projects/storage/config") {
      return jsonResponse({ ...serverState });
    }
    throw new Error(`unexpected fetch: ${method} ${url}`);
  });
  vi.stubGlobal("fetch", stub);
  return stub;
}

function listGets(stub: ReturnType<typeof vi.fn>) {
  return stub.mock.calls.filter(
    ([url, init]) =>
      (init?.method ?? "GET").toUpperCase() === "GET" &&
      url === "/api/projects/storage/config",
  );
}

// ---- helpers ---------------------------------------------------------------

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return ({ children }: { children: React.ReactNode }) =>
    createElement(QueryClientProvider, { client: qc }, children);
}

function renderSection() {
  return render(createElement(ProjectStorageSection), { wrapper: makeWrapper() });
}

// ---- setup / teardown ------------------------------------------------------

let fetchStub: ReturnType<typeof vi.fn>;

beforeEach(() => {
  serverState = defaultServerConfig();
  // Force the live branch in @/api/client (BASE is captured at module load, so
  // intercepting the isLive() function via vi.mock is the only reliable seam —
  // same approach as McpSection.live.test.tsx and agent.upload.test.ts).
  isLiveMock.mockReturnValue(true);
  fetchStub = installFetch();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// ---- tests -----------------------------------------------------------------

describe("ProjectStorageSection — surfaces the resolved default path (e4ux)", () => {
  it("renders the effective default path + '(default)' marker when projects_root is empty", async () => {
    renderSection();

    // Wait for the GET to fire and the response to render. The section starts
    // in "Loading…" (isLoading=true); once the GET resolves the active-root
    // data-testid appears, and THAT is what the assertions target.
    const activeRoot = await screen.findByTestId("projects-active-root", {}, { timeout: 3000 });

    // The server was queried over the real fetch path (not a module mock).
    expect(listGets(fetchStub).length).toBe(1);

    // The "Saving to:" line surfaces the EFFECTIVE root the server resolved —
    // the auto-created default — not the empty input.
    expect(activeRoot).toHaveTextContent("/home/you/.local/share/disco/projects");
    expect(activeRoot).toHaveTextContent("Saving to:");

    // The "(default)" marker makes it transparent that the path is server-chosen,
    // not user-typed.
    const marker = screen.getByTestId("projects-default-marker");
    expect(marker).toHaveTextContent("(default)");

    // The input itself stays empty (placeholder visible) so the user can
    // override without first deleting the rendered default.
    const input = screen.getByLabelText("Projects root") as HTMLInputElement;
    expect(input.value).toBe("");
    expect(input.placeholder).toBe("/home/you/disco-projects");
  });

  it("renders the configured path WITHOUT the '(default)' marker when projects_root is set", async () => {
    serverState = explicitServerConfig("/var/data/disco-projects");
    renderSection();

    // Same wait pattern as the default-case test: the data-testid only appears
    // once the GET resolves and the component re-renders.
    const activeRoot = await screen.findByTestId("projects-active-root", {}, { timeout: 3000 });

    // Active root is the configured path — NOT the auto default.
    expect(activeRoot).toHaveTextContent("/var/data/disco-projects");
    expect(activeRoot).toHaveTextContent("Saving to:");

    // The "(default)" marker is GONE — the user has made an explicit choice.
    expect(screen.queryByTestId("projects-default-marker")).not.toBeInTheDocument();

    // The input is prefilled with the configured path.
    const input = screen.getByLabelText("Projects root") as HTMLInputElement;
    expect(input.value).toBe("/var/data/disco-projects");
  });
});
