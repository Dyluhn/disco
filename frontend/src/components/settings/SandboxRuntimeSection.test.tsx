import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SandboxRuntimeSection } from "./SandboxRuntimeSection";

const useSandboxConfig = vi.fn();

vi.mock("@/hooks/useModels", () => ({
  useSandboxConfig: () => useSandboxConfig(),
}));

vi.mock("./SandboxSection", () => ({
  SandboxSection: () => <h3>Sandbox</h3>,
}));

vi.mock("./LiveBrowserSection", () => ({
  LiveBrowserSection: () => <h3>Live browser</h3>,
}));

describe("SandboxRuntimeSection", () => {
  beforeEach(() => {
    useSandboxConfig.mockReset();
  });

  it("keeps the gVisor-only control out of an unsupported runtime", () => {
    useSandboxConfig.mockReturnValue({ data: { backend: "local" } });
    render(<SandboxRuntimeSection />);

    expect(screen.getByRole("heading", { name: "Sandbox" })).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Live browser" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText(/settings appear here when gVisor is active/i),
    ).toBeInTheDocument();
  });

  it("reveals live-browser controls under a compatible sandbox", () => {
    useSandboxConfig.mockReturnValue({ data: { backend: "gvisor" } });
    render(<SandboxRuntimeSection />);

    expect(screen.getByRole("heading", { name: "Sandbox" })).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Live browser" }),
    ).toBeInTheDocument();
    expect(document.getElementById("live-browser")).toContainElement(
      screen.getByRole("heading", { name: "Live browser" }),
    );
  });
});
