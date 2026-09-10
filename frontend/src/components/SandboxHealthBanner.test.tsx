/**
 * SandboxHealthBanner — surfaces an UNREACHABLE sandbox in the app shell BEFORE a doomed
 * run. The 2026-06-23 outage was discovered only by every run failing with a cryptic SSH
 * error; this banner makes the broken sandbox visible up-front with the backend + the real
 * reason + a Settings affordance. Hidden when reachable / before the first verdict.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { SandboxHealth } from "@/types/sandbox";
import { useSandboxHealth } from "@/hooks/useModels";
import { SandboxHealthBanner } from "./SandboxHealthBanner";

vi.mock("@/hooks/useModels", () => ({ useSandboxHealth: vi.fn() }));
const mockHealth = vi.mocked(useSandboxHealth);

function wrap() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <SandboxHealthBanner />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function asQuery(data: SandboxHealth | undefined) {
  return { data } as ReturnType<typeof useSandboxHealth>;
}

afterEach(() => vi.clearAllMocks());

describe("SandboxHealthBanner", () => {
  it("renders the backend + reason + Settings link when unreachable", () => {
    mockHealth.mockReturnValue(
      asQuery({
        reachable: false,
        backend: "gvisor",
        detail: "gvisor sandbox host ssh://sandbox@ unreachable: ssh: Could not resolve hostname :",
      }),
    );
    wrap();
    const banner = screen.getByRole("alert");
    expect(banner).toHaveAttribute("data-sandbox-unreachable", "gvisor");
    expect(banner).toHaveTextContent(/gvisor sandbox unreachable/i);
    expect(banner).toHaveTextContent(/Could not resolve hostname/);
    const link = screen.getByRole("link", { name: /settings.*sandbox/i });
    expect(link).toHaveAttribute("href", "/settings");
  });

  it("is hidden when the sandbox is reachable", () => {
    mockHealth.mockReturnValue(asQuery({ reachable: true, backend: "process", detail: "" }));
    wrap();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("is hidden before the first verdict (no flash of a false alarm)", () => {
    mockHealth.mockReturnValue(asQuery(undefined));
    wrap();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
