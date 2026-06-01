import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { streamTiming } from "@/api/research";
import App from "@/App";

describe("Research surface (integration + streaming reconcile)", () => {
  beforeEach(() => {
    // collapse stream cadence so the run completes within the test
    streamTiming.ttft = 0;
    streamTiming.token = 0;
    streamTiming.blockGap = 0;
  });

  it("goes empty → query → a finished, grounded, structured answer", async () => {
    const user = userEvent.setup();
    render(<App />);

    // empty state (wordmark appears in header + hero)
    expect(screen.getAllByText("perpleximanus").length).toBeGreaterThan(0);

    await user.type(screen.getByPlaceholderText(/ask anything/i), "How does RRF work?");
    await user.keyboard("{Enter}");

    // Wait for the FINISHED signal: follow-up pills render only once the run
    // completes (phase !== "running" and the authoritative answer is set).
    await waitFor(
      () => expect(screen.getByRole("button", { name: /How is k chosen/i })).toBeInTheDocument(),
      { timeout: 3000 },
    );

    // The table caption + code arrive via block/final frames — never tokens.
    // Seeing them proves the UI reconciled to the authoritative answer.
    expect(screen.getByText(/RRF vs\. learning-to-rank/i)).toBeInTheDocument();
    // prose reconciled (final text present)
    expect(screen.getByText(/without any score normalization/i)).toBeInTheDocument();
    // the question rendered as a document title
    expect(screen.getByRole("heading", { name: /How does RRF work\?/i })).toBeInTheDocument();
  });

  it("renders verification verdicts as citations (the unsupported claim is marked)", async () => {
    const user = userEvent.setup();
    render(<App />);
    await user.type(screen.getByPlaceholderText(/ask anything/i), "RRF?");
    await user.keyboard("{Enter}");
    // wait for finished (citations resolve only once the final answer is set)
    await waitFor(
      () => expect(screen.getByRole("button", { name: /How is k chosen/i })).toBeInTheDocument(),
      { timeout: 3000 },
    );
    const article = screen.getByRole("article");
    // an unsupported claim's citation carries the verdict in its label
    expect(
      within(article).getByRole("button", { name: /Not supported by the cited source/i }),
    ).toBeInTheDocument();
  });
});
