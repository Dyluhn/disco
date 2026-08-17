import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import type { Passage } from "@/types/grounded";
import { Citation } from "./Citation";

const passage: Passage = {
  id: "p1",
  source_url: "https://example.com/paper",
  source_title: "A paper on rank fusion",
  text: "The corroborating sentence the reader can verify.",
};

describe("Citation", () => {
  beforeEach(() => {
    // coarse pointer → the chip opens a bottom-sheet (deterministic in jsdom)
    window.matchMedia = ((q: string) =>
      ({
        matches: q.includes("coarse"),
        media: q,
        addEventListener: () => {},
        removeEventListener: () => {},
        onchange: null,
        addListener: () => {},
        removeListener: () => {},
        dispatchEvent: () => false,
      }) as unknown as MediaQueryList) as typeof window.matchMedia;
  });

  it("carries the verdict in its accessible label (chroma = meaning)", () => {
    render(<Citation n={3} passage={passage} verdict="unsupported" />);
    expect(
      screen.getByRole("button", { name: /Source 3.*Not supported by the cited source/i }),
    ).toBeInTheDocument();
  });

  it("opens a card with the corroborating snippet (verify without leaving)", async () => {
    render(<Citation n={1} passage={passage} verdict="supported" />);
    await userEvent.click(screen.getByRole("button", { name: /Source 1/i }));
    expect(screen.getByText(/corroborating sentence the reader can verify/i)).toBeInTheDocument();
    expect(screen.getByText("example.com")).toBeInTheDocument(); // clean domain
  });
});
