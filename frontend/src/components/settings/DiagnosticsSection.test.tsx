/**
 * Settings → Diagnostics renders fetched facts only: build identity, both servers,
 * the sandbox probe and the doctor's local checks; a failed fetch is shown as such;
 * the copy button writes the payload as JSON.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AppServerHealth, Diagnostics } from "@/types/diagnostics";
import { DiagnosticsSection } from "./DiagnosticsSection";

const diagnostics: Diagnostics = {
  generated_at: "2026-09-12T00:00:00Z",
  version: "v0.2.0 (3f9c1a2)",
  build: { tag: "v0.2.0", commit: "3f9c1a2", source: "image" },
  agent_server: { status: "ok", version: "v0.2.0 (3f9c1a2)", build: { tag: "v0.2.0", commit: "3f9c1a2", source: "image" }, checks: { store: "ok" } },
  sandbox: { reachable: false, backend: "podman", detail: "podman sandbox host unix:///var/run/docker.sock unreachable" },
  checks: [
    { name: "data disk", status: "PASS", detail: "41.2 GB free at /data" },
    { name: "secret key", status: "WARN", detail: "none of DISCO_SECRET_KEY / DISCO_AUTH_SECRET set" },
  ],
  env: { DISCO_SECRET_KEY: "***REDACTED***" },
};
const appHealth: AppServerHealth = { status: "ok", service: "app-server", version: "v0.2.0 (3f9c1a2)", build: diagnostics.build };

const state = vi.hoisted(() => ({
  diagnostics: { data: undefined as Diagnostics | undefined, isPending: false, isError: false, error: null as unknown },
  app: { data: undefined as AppServerHealth | undefined, isPending: false, isError: false, error: null as unknown },
}));

vi.mock("@/hooks/useDiagnostics", () => ({
  useDiagnostics: () => state.diagnostics,
  useAppServerHealth: () => state.app,
}));

describe("DiagnosticsSection", () => {
  beforeEach(() => {
    state.diagnostics = { data: diagnostics, isPending: false, isError: false, error: null };
    state.app = { data: appHealth, isPending: false, isError: false, error: null };
  });

  it("shows the build and one row per fact with the fetched detail", () => {
    render(<DiagnosticsSection />);
    expect(screen.getByText("Build v0.2.0 (3f9c1a2) (image)")).toBeInTheDocument();
    const list = screen.getByRole("list", { name: /Diagnostic checks/i });
    const rows = within(list).getAllByRole("listitem");
    expect(rows.map((r) => r.querySelector("[data-check-status]")?.textContent)).toEqual([
      "PASS", "PASS", "FAIL", "PASS", "WARN",
    ]);
    expect(within(list).getByText(/podman: podman sandbox host unix:\/\/\/var\/run\/docker.sock unreachable/)).toBeInTheDocument();
    expect(within(list).getByText(/app-server/)).toBeInTheDocument();
    expect(within(list).getByText(/41.2 GB free at \/data/)).toBeInTheDocument();
  });

  it("shows a fetch failure instead of inventing a green", () => {
    state.diagnostics = { data: undefined, isPending: false, isError: true, error: new Error("HTTP 502") };
    render(<DiagnosticsSection />);
    expect(screen.getByRole("alert")).toHaveTextContent("Diagnostics unavailable: HTTP 502");
    expect(screen.getByRole("button", { name: /Copy diagnostics/i })).toBeDisabled();
  });

  it("copies the payload as JSON", async () => {
    const writeText = vi.fn(() => Promise.resolve());
    Object.assign(navigator, { clipboard: { writeText } });
    render(<DiagnosticsSection />);
    await userEvent.click(screen.getByRole("button", { name: /Copy diagnostics/i }));
    await waitFor(() => expect(screen.getByRole("button", { name: /Copied/i })).toBeInTheDocument());
    const payload = JSON.parse(writeText.mock.calls[0][0] as string);
    expect(payload.diagnostics.version).toBe("v0.2.0 (3f9c1a2)");
    expect(payload.app_server.service).toBe("app-server");
    expect(JSON.stringify(payload)).not.toContain("sk-");
  });

  it("names the doctor command for the bundle", () => {
    render(<DiagnosticsSection />);
    expect(screen.getByText(/python -m disco.agent_server.doctor --bundle/)).toBeInTheDocument();
  });
});
