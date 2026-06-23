/**
 * DeckExportBar — the SHARED deck export bar (W-16/W-19).
 *
 * Asserts the honest-affordance contract:
 *  - Renders real `pptx` AND `html` download links to the /deck/export route.
 *  - Each link carries the jailed `path={base}` + the selected `template` param.
 *  - The `pdf` link (W-22) is shown ONLY when the active sandbox backend is a
 *    container (gvisor/local/podman); on the host `process` backend it is HIDDEN
 *    (the route would 409) — no false affordance.
 *  - The Theme picker is first-class; changing it re-points BOTH links.
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DeckExportBar } from "./DeckExportBar";

// Deterministic templates (Disco default + Midnight) so we don't hit a server.
vi.mock("@/hooks/useTemplates", () => ({
  useTemplates: () => [
    { id: "disco-light", name: "disco", mode: "light", label: "Disco", description: "Default", accent: "#4077a3", bg: "#fcfcfa", default: true },
    { id: "midnight-dark", name: "midnight", mode: "dark", label: "Midnight", description: "Dark", accent: "#d9a441", bg: "#0d1017", default: false },
  ],
}));

// Live agent so DeckExportBar fetches /state to read the sandbox backend.
vi.mock("@/api/client", () => ({
  agentLive: () => true,
  agentHttpBase: () => "http://agent.test",
}));

const CID = "conv_deck_export_1";
const BASE = "q3_pitch";

/** Stub GET /conversations/{cid}/state to report a chosen sandbox backend. */
function mockBackend(backend: string | null): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ json: async () => ({ sandbox_backend: backend }) })) as unknown as typeof fetch,
  );
}

beforeEach(() => mockBackend("process"));
afterEach(() => vi.unstubAllGlobals());

function exportLinks(): HTMLAnchorElement[] {
  return screen
    .getAllByRole("link")
    .filter((a) => (a as HTMLAnchorElement).getAttribute("href")?.includes("/deck/export")) as HTMLAnchorElement[];
}

describe("DeckExportBar", () => {
  it("renders real pptx + html export links carrying path + template params", async () => {
    render(<DeckExportBar conversationId={CID} base={BASE} />);

    const pptx = await screen.findByRole("link", { name: /PowerPoint/i });
    const html = screen.getByRole("link", { name: /Web page/i });

    for (const link of [pptx, html]) {
      const href = link.getAttribute("href")!;
      expect(href).toContain(`/conversations/${CID}/deck/export`);
      expect(href).toContain(`path=${BASE}`);
      expect(href).toContain("template=disco-light");
    }
    expect(pptx.getAttribute("href")).toContain("fmt=pptx");
    expect(html.getAttribute("href")).toContain("fmt=html");
  });

  it("HIDES the pdf link on the host process backend (no container → no false affordance)", async () => {
    mockBackend("process");
    render(<DeckExportBar conversationId={CID} base={BASE} />);
    // Let the /state fetch settle, then assert no fmt=pdf affordance appeared.
    await waitFor(() => expect(screen.getByRole("link", { name: /PowerPoint/i })).toBeInTheDocument());
    for (const a of exportLinks()) {
      expect(a.getAttribute("href")).not.toContain("fmt=pdf");
    }
    expect(screen.queryByRole("link", { name: /pdf/i })).not.toBeInTheDocument();
  });

  it("SHOWS the themed pdf link on a container backend (gvisor)", async () => {
    mockBackend("gvisor");
    render(<DeckExportBar conversationId={CID} base={BASE} />);
    const pdf = await screen.findByRole("link", { name: /PDF/i });
    const href = pdf.getAttribute("href")!;
    expect(href).toContain(`/conversations/${CID}/deck/export`);
    expect(href).toContain(`path=${BASE}`);
    expect(href).toContain("template=disco-light");
    expect(href).toContain("fmt=pdf");
  });

  it("exposes a first-class Theme picker that re-points BOTH links when changed", async () => {
    const user = userEvent.setup();
    render(<DeckExportBar conversationId={CID} base={BASE} />);

    const select = screen.getByLabelText(/Theme/i) as HTMLSelectElement;
    expect(select.value).toBe("disco-light");

    await user.selectOptions(select, "midnight-dark");

    const pptx = screen.getByRole("link", { name: /PowerPoint/i });
    const html = screen.getByRole("link", { name: /Web page/i });
    expect(pptx.getAttribute("href")).toContain("template=midnight-dark");
    expect(html.getAttribute("href")).toContain("template=midnight-dark");
  });

  it("shows the optional title heading when provided", async () => {
    const { container } = render(
      <DeckExportBar conversationId={CID} base={BASE} title="Q3 Pitch Deck" />,
    );
    await screen.findByRole("link", { name: /PowerPoint/i });
    const bar = container.querySelector('[data-disco-control="build.deck-export-bar"]')!;
    expect(within(bar as HTMLElement).getByText("Q3 Pitch Deck")).toBeInTheDocument();
  });
});
