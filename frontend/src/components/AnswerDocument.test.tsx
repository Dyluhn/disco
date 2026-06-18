/**
 * WALK-01 (A1): AnswerDocument streaming text renders through <Markdown>
 * instead of raw text, so partial markdown (e.g. **bold**) formats in-flight.
 *
 * F3 — cid threading contract: AnswerDocument forwards cid to BlockView so
 * sheet/slides blocks carry a working download link when (and only when) a
 * real cid is present. No cid = no download (no false affordance).
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { AnswerBlock } from "@/types/grounded";
import { AnswerDocument } from "./AnswerDocument";

describe("AnswerDocument — streaming block", () => {
  const STREAMING_ID = "block-stream-1";

  it("renders plain streaming text without a markdown token", () => {
    render(
      <AnswerDocument
        blocks={[]}
        partial={{ [STREAMING_ID]: "Hello world" }}
        streamingBlockId={STREAMING_ID}
        answer={null}
      />,
    );
    expect(screen.getByText("Hello world")).toBeInTheDocument();
  });

  it("WALK-01: renders **bold** markdown in the streaming block as <strong>, not raw text", () => {
    render(
      <AnswerDocument
        blocks={[]}
        partial={{ [STREAMING_ID]: "**bold** text" }}
        streamingBlockId={STREAMING_ID}
        answer={null}
      />,
    );
    // react-markdown should turn **bold** into a <strong> element
    const bold = screen.getByText("bold");
    expect(bold.tagName).toBe("STRONG");
    // The raw asterisks must not appear
    expect(screen.queryByText(/\*\*bold\*\*/)).not.toBeInTheDocument();
  });

  it("WALK-01: the blinking caret is present alongside the markdown output", () => {
    const { container } = render(
      <AnswerDocument
        blocks={[]}
        partial={{ [STREAMING_ID]: "some text" }}
        streamingBlockId={STREAMING_ID}
        answer={null}
      />,
    );
    // Caret = the animate-pulse span appended after Markdown
    const caret = container.querySelector(".animate-pulse");
    expect(caret).toBeInTheDocument();
  });

  it("does not render the streaming block once it has moved into blocks[]", () => {
    // When the block graduates from partial → blocks[], streamingBlockId still
    // refers to it but the "in flight" guard drops it (blocks.some(b => b.id ===
    // streamingBlockId) is true → streamingText = undefined).
    const { container } = render(
      <AnswerDocument
        blocks={[
          { id: STREAMING_ID, kind: "prose", text: "x", cited_passage_ids: [] },
        ]}
        partial={{ [STREAMING_ID]: "**partial**" }}
        streamingBlockId={STREAMING_ID}
        answer={null}
      />,
    );
    // The raw **partial** markdown must not appear as streaming text —
    // the block graduated and only the structured BlockView renders it.
    expect(screen.queryByText(/\*\*partial\*\*/)).not.toBeInTheDocument();
    // No caret — the streaming container is gone
    expect(container.querySelector(".animate-pulse")).not.toBeInTheDocument();
  });
});

// ---- F3: cid threading — block download in AnswerDocument -------------------

describe("AnswerDocument — F3 cid threading for block downloads", () => {
  const sheetBlock: AnswerBlock = {
    kind: "sheet",
    id: "sh1",
    title: "Revenue Model",
    filename: "revenue.xlsx",
    sheet_names: ["Income", "Costs"],
    formulas_evaluated: false,
  };

  it("F3: sheet block without cid renders NO download link (no false affordance)", () => {
    const { container } = render(
      <AnswerDocument
        blocks={[sheetBlock]}
        partial={{}}
        streamingBlockId={null}
        answer={null}
      />,
    );
    // No download anchor — the cid is absent, so the artifact route is unknown.
    expect(container.querySelector("a[download]")).toBeNull();
  });

  it("F3: sheet block with cid=null renders NO download link (explicit null = absent)", () => {
    const { container } = render(
      <AnswerDocument
        blocks={[sheetBlock]}
        partial={{}}
        streamingBlockId={null}
        answer={null}
        cid={null}
      />,
    );
    expect(container.querySelector("a[download]")).toBeNull();
  });

  it("F3: sheet block with a real cid renders a download link to the artifact route", () => {
    const CID = "conv_f3_sheet_1";
    render(
      <AnswerDocument
        blocks={[sheetBlock]}
        partial={{}}
        streamingBlockId={null}
        answer={null}
        cid={CID}
      />,
    );
    // The SheetDownload anchor must point at the declared-artifact route so a
    // real fetch can succeed (no 404 false affordance).
    const link = screen.getByRole("link", { name: /Revenue Model/i });
    expect(link).toHaveAttribute("download");
    expect(link.getAttribute("href")).toContain(
      `/conversations/${CID}/artifacts/revenue.xlsx`,
    );
  });

  it("F3: cid is forwarded verbatim (the URL embeds the exact cid string)", () => {
    const CID = "conv-my-exact-cid-abc123";
    render(
      <AnswerDocument
        blocks={[sheetBlock]}
        partial={{}}
        streamingBlockId={null}
        answer={null}
        cid={CID}
      />,
    );
    const link = screen.getByRole("link", { name: /Revenue Model/i });
    expect(link.getAttribute("href")).toMatch(
      new RegExp(`/conversations/${CID}/artifacts/`),
    );
  });
});
