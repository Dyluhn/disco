/**
 * Regression: ```chart fences must render as REAL charts, not code blocks.
 * Deep-research report sections arrive as raw markdown (the synthesis prompt embeds
 * charts as ```chart fenced JSON); the Markdown renderer lifts them into
 * ChartBlockComponent. Malformed payloads fall through to an honest code fence.
 */
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Markdown } from "./Markdown";

const CHART_MD = [
  "Before text.",
  "```chart",
  JSON.stringify({
    chart_type: "bar",
    title: "T",
    data: [
      { label: "a", value: 1 },
      { label: "b", value: 2 },
    ],
  }),
  "```",
  "After text.",
].join("\n");

describe("Markdown ```chart lift", () => {
  it("renders a chart canvas, not a code block", () => {
    const { container } = render(<Markdown>{CHART_MD}</Markdown>);
    expect(container.querySelector("canvas")).toBeTruthy();
    expect(container.querySelector("pre")).toBeNull();
  });

  it("malformed chart JSON falls back to a code fence (no crash, no fake chart)", () => {
    const bad = "```chart\n{not json at all\n```";
    const { container } = render(<Markdown>{bad}</Markdown>);
    expect(container.querySelector("canvas")).toBeNull();
    expect(container.querySelector("pre")).toBeTruthy();
  });

  it("ordinary code fences still render as code", () => {
    const code = "```python\nprint('hi')\n```";
    const { container } = render(<Markdown>{code}</Markdown>);
    expect(container.querySelector("pre")).toBeTruthy();
    expect(container.querySelector("canvas")).toBeNull();
  });
});
