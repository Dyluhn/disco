/**
 * DeckExportBar — the SHARED deck export bar (W-16/W-19).
 *
 * Asserts the honest-affordance contract:
 *  - Renders real `pptx` AND `html` download links to the /deck/export route.
 *  - Each link carries the jailed `path={base}` + the selected `template` param.
 *  - There is NO `pdf` link (backend deck export supports only pptx+html today;
 *    PDF lands with W-22 — a PDF link now would be a 404 false affordance).
 *  - The Theme picker is first-class; changing it re-points BOTH links.
 */

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { DeckExportBar } from "./DeckExportBar";

// Deterministic templates (Disco default + Midnight) so we don't hit a server.
vi.mock("@/hooks/useTemplates", () => ({
  useTemplates: () => [
    { id: "disco-light", name: "disco", mode: "light", label: "Disco", description: "Default", accent: "#4077a3", bg: "#fcfcfa", default: true },
    { id: "midnight-dark", name: "midnight", mode: "dark", label: "Midnight", description: "Dark", accent: "#d9a441", bg: "#0d1017", default: false },
  ],
}));

const CID = "conv_deck_export_1";
const BASE = "q3_pitch";

function exportLinks(): HTMLAnchorElement[] {
  return screen
    .getAllByRole("link")
    .filter((a) => (a as HTMLAnchorElement).getAttribute("href")?.includes("/deck/export")) as HTMLAnchorElement[];
}

describe("DeckExportBar", () => {
  it("renders real pptx + html export links carrying path + template params", () => {
    render(<DeckExportBar conversationId={CID} base={BASE} />);

    const pptx = screen.getByRole("link", { name: /PowerPoint/i });
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

  it("renders NO pdf link (W-22 gates PDF deck export — no false affordance)", () => {
    render(<DeckExportBar conversationId={CID} base={BASE} />);
    // No export link should request fmt=pdf, and no "PDF" affordance should exist.
    for (const a of exportLinks()) {
      expect(a.getAttribute("href")).not.toContain("fmt=pdf");
    }
    expect(screen.queryByRole("link", { name: /pdf/i })).not.toBeInTheDocument();
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

  it("shows the optional title heading when provided", () => {
    const { container } = render(
      <DeckExportBar conversationId={CID} base={BASE} title="Q3 Pitch Deck" />,
    );
    const bar = container.querySelector('[data-disco-control="build.deck-export-bar"]')!;
    expect(within(bar as HTMLElement).getByText("Q3 Pitch Deck")).toBeInTheDocument();
  });
});
