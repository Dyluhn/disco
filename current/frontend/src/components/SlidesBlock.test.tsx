import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { BlockView } from "./blocks";
import type { AnswerBlock } from "@/types/grounded";

// SlidesBlock is an HONEST PREVIEW + NAV card. The .html / .pdf / .pptx is a
// real workspace artifact delivered via DeliverableEvent (which holds the
// cid); without a cid threaded down, a self-fetching download button would
// always 404 (a false affordance).
//
// D2: when a cid IS threaded in, the in-block download appears and points at
// the declared-artifact route — reusing ActivityFeed's SlidesDownload (not a
// fork). When NO cid is present, the button stays absent. The prev/next nav
// walks the inline `slides` list, with a counter fallback when no per-slide
// content was provided. These tests assert both halves of that contract.

const baseSlides: AnswerBlock = {
  kind: "slides",
  id: "d1",
  title: "Q3 Pitch Deck",
  filename: "q3_pitch.pdf",
  format: "pdf",
  slide_count: 3,
  slides: [
    { title: "Cover", content: "Perpleximanus — Q3 2026" },
    { title: "Problem", content: "Slow slide rendering across stacks." },
    { title: "Ask", content: "Series A, $8M." },
  ],
  renderer: "marp",
};

describe("SlidesBlock component", () => {
  // ---- header / metadata --------------------------------------------------

  it("renders the deck title, filename, format, and slide count", () => {
    const { container } = render(<BlockView block={baseSlides} answer={null} />);

    expect(screen.getByText("Q3 Pitch Deck")).toBeInTheDocument();
    // Filename appears in the header (mono) and in the provenance footer.
    expect(screen.getAllByText("q3_pitch.pdf").length).toBeGreaterThanOrEqual(1);
    // Format is uppercased in the header — "PDF" surrounded by "·" separators.
    // Use textContent + regex to avoid matching the filename's lowercase
    // extension AND to ignore incidental text inside SVG paths.
    expect(container.textContent).toMatch(/· PDF ·/);
    expect(container.textContent).toMatch(/3 slides/);
  });

  it("renders the singular '1 slide' for a single-slide deck", () => {
    const block: AnswerBlock = {
      ...baseSlides,
      filename: "one.pdf",
      slide_count: 1,
      slides: [{ title: "Hello", content: "World" }],
    };
    const { container } = render(<BlockView block={block} answer={null} />);

    // Singular form — no trailing "s" before the "·" separator or end.
    expect(container.textContent).toMatch(/1 slide(?!s)/);
  });

  it("shows the renderer in the provenance footer (Marp vs fallback)", () => {
    const fallback: AnswerBlock = { ...baseSlides, filename: "fallback.html", renderer: "fallback" };
    const { rerender, container } = render(<BlockView block={baseSlides} answer={null} />);
    expect(container.textContent).toMatch(/rendered by\s+Marp/i);

    rerender(<BlockView block={fallback} answer={null} />);
    expect(container.textContent).toMatch(/rendered by\s+the HTML fallback renderer/i);
  });

  it("shows the deck is saved to the workspace (provenance, not a download)", () => {
    render(<BlockView block={baseSlides} answer={null} />);
    expect(screen.getByText(/saved to the workspace/i)).toBeInTheDocument();
  });

  // ---- navigation ---------------------------------------------------------

  it("starts on slide 1 of N and shows the first slide's content", () => {
    render(<BlockView block={baseSlides} answer={null} />);

    // Counter
    expect(screen.getByText("1")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    // First slide body
    expect(screen.getByText("Perpleximanus — Q3 2026")).toBeInTheDocument();
  });

  it("advances to the next slide when Next is clicked", async () => {
    const user = userEvent.setup();
    render(<BlockView block={baseSlides} answer={null} />);

    await user.click(screen.getByRole("button", { name: /next slide/i }));

    // The slide 2 content becomes visible; slide 1's content is gone.
    expect(screen.getByText("Slow slide rendering across stacks.")).toBeInTheDocument();
    expect(screen.queryByText("Perpleximanus — Q3 2026")).not.toBeInTheDocument();
  });

  it("returns to the previous slide when Prev is clicked", async () => {
    const user = userEvent.setup();
    render(<BlockView block={baseSlides} answer={null} />);

    // Step forward twice, then back once → slide 2 of 3.
    await user.click(screen.getByRole("button", { name: /next slide/i }));
    await user.click(screen.getByRole("button", { name: /next slide/i }));
    await user.click(screen.getByRole("button", { name: /previous slide/i }));

    expect(screen.getByText("Slow slide rendering across stacks.")).toBeInTheDocument();
  });

  it("wraps past the last slide back to the first", async () => {
    const user = userEvent.setup();
    render(<BlockView block={baseSlides} answer={null} />);

    // 3 Next clicks from slide 1 → slide 4 wraps to slide 1.
    await user.click(screen.getByRole("button", { name: /next slide/i }));
    await user.click(screen.getByRole("button", { name: /next slide/i }));
    await user.click(screen.getByRole("button", { name: /next slide/i }));

    expect(screen.getByText("Perpleximanus — Q3 2026")).toBeInTheDocument();
  });

  it("wraps past the first slide back to the last", async () => {
    const user = userEvent.setup();
    render(<BlockView block={baseSlides} answer={null} />);

    // One Prev from slide 1 wraps to slide 3 (the last).
    await user.click(screen.getByRole("button", { name: /previous slide/i }));

    expect(screen.getByText("Series A, $8M.")).toBeInTheDocument();
  });

  it("degrades to a counter (no per-slide content) when slides is empty", async () => {
    const user = userEvent.setup();
    const block: AnswerBlock = {
      ...baseSlides,
      filename: "no-content.pdf",
      slides: [],
    };
    render(<BlockView block={block} answer={null} />);

    // No inline slide content → a placeholder line is shown.
    expect(screen.getByText(/slide 1 of 3/i)).toBeInTheDocument();
    // Nav still works.
    await user.click(screen.getByRole("button", { name: /next slide/i }));
    expect(screen.getByText(/slide 2 of 3/i)).toBeInTheDocument();
  });

  // ---- no false affordance (cid absent) -----------------------------------

  it("renders NO download button when cid is absent (no false affordance)", () => {
    const { container } = render(<BlockView block={baseSlides} answer={null} />);

    // A self-fetching download link without a cid would always 404 — never
    // render one. The honest download lives in the Build/Agent ActivityFeed
    // (which has a cid).
    expect(container.querySelector("a[download]")).toBeNull();
    expect(screen.queryByRole("link", { name: /q3 pitch deck/i })).toBeNull();
  });

  it("renders NO download button when cid is explicitly null (no false affordance)", () => {
    const { container } = render(<BlockView block={baseSlides} answer={null} cid={null} />);
    expect(container.querySelector("a[download]")).toBeNull();
  });

  // ---- cid-threading contract (D2) ---------------------------------------

  it("renders a working in-block download when a cid is threaded in (D2)", () => {
    const CID = "conv_slides_block_1";
    render(<BlockView block={baseSlides} answer={null} cid={CID} />);

    // The download link reuses ActivityFeed's SlidesDownload verbatim (same
    // <a download>, same declared-artifact route). It must resolve against
    // /conversations/{cid}/artifacts/{filename}.
    const link = screen.getByRole("link", { name: /Q3 Pitch Deck/i });
    expect(link).toHaveAttribute("download");
    expect(link.getAttribute("href")).toContain(`/conversations/${CID}/artifacts/q3_pitch.pdf`);
  });

  it("preserves subdir slashes in the filename (encodeURI, not encodeURIComponent)", () => {
    const block: AnswerBlock = {
      ...baseSlides,
      filename: "decks/q3/pitch.pdf",
    };
    const CID = "conv_slides_subdir";
    render(<BlockView block={block} answer={null} cid={CID} />);

    const link = screen.getByRole("link", { name: /Q3 Pitch Deck/i });
    expect(link.getAttribute("href")).toContain(
      `/conversations/${CID}/artifacts/decks/q3/pitch.pdf`,
    );
  });

  it("in-block download and ActivityFeed download use the same href shape", () => {
    // Lock the D2 contract: SlidesBlock reuses SlidesDownload, not a fork.
    // The href must match the ActivityFeed's expected URL byte-for-byte.
    const CID = "conv_slides_shared";
    render(<BlockView block={baseSlides} answer={null} cid={CID} />);

    const link = screen.getByRole("link", { name: /Q3 Pitch Deck/i });
    expect(link.getAttribute("href")).toMatch(
      new RegExp(`/conversations/${CID}/artifacts/q3_pitch\\.pdf$`),
    );
    expect(link).toHaveAttribute("download");
  });
});
