import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { streamTiming } from "@/api/research";
import App from "@/App";

// This is the heaviest suite in the frontend (full <App/> render + the whole
// streaming-reconcile pipeline, twice). `streamTiming` is collapsed to 0 below,
// so these budgets gate NO latency SLA — they only bound React scheduling. Run
// isolated they settle in ~2s; under default 42-file vitest parallelism, CPU
// contention pushes the reconcile past the old 3s `waitFor` (~5.2s observed),
// the lone source of the "passes alone, times out in the full suite" flake.
// Budgets are widened HERE (scoped to this file) rather than via a global
// `testTimeout` bump, so every other suite still fails fast on a real hang.
// Do NOT tighten these back to 3s — that reintroduces the contention flake.
const FINISH_TIMEOUT = 15_000; // waitFor budget for the run to reach FINISHED
const TEST_TIMEOUT = 20_000; // per-test ceiling (> FINISH_TIMEOUT, headroom for contention)
const SURFACE_LOAD_TIMEOUT = 10_000; // cold React.lazy import under parallel hermetic load

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
    expect(screen.getAllByText("Disco").length).toBeGreaterThan(0);

    await user.type(
      await screen.findByPlaceholderText(/ask anything/i, {}, { timeout: SURFACE_LOAD_TIMEOUT }),
      "How does RRF work?",
    );
    await user.keyboard("{Enter}");

    // Wait for the FINISHED signal: follow-up pills render only once the run
    // completes (phase !== "running" and the authoritative answer is set).
    await waitFor(
      () => expect(screen.getByRole("button", { name: /How is k chosen/i })).toBeInTheDocument(),
      { timeout: FINISH_TIMEOUT },
    );

    // The table caption + code arrive via block/final frames — never tokens.
    // Seeing them proves the UI reconciled to the authoritative answer.
    expect(screen.getByText(/RRF vs\. learning-to-rank/i)).toBeInTheDocument();
    // prose reconciled (final text present)
    expect(screen.getByText(/without any score normalization/i)).toBeInTheDocument();
    // the question rendered as a document title
    expect(screen.getByRole("heading", { name: /How does RRF work\?/i })).toBeInTheDocument();
  }, TEST_TIMEOUT);

  it("renders verification verdicts as citations (the unsupported claim is marked)", async () => {
    const user = userEvent.setup();
    render(<App />);
    await user.type(
      await screen.findByPlaceholderText(/ask anything/i, {}, { timeout: SURFACE_LOAD_TIMEOUT }),
      "RRF?",
    );
    await user.keyboard("{Enter}");
    // wait for finished (citations resolve only once the final answer is set)
    await waitFor(
      () => expect(screen.getByRole("button", { name: /How is k chosen/i })).toBeInTheDocument(),
      { timeout: FINISH_TIMEOUT },
    );
    const article = screen.getByRole("article");
    // an unsupported claim's citation carries the verdict in its label
    expect(
      within(article).getByRole("button", { name: /Not supported by the cited source/i }),
    ).toBeInTheDocument();
  }, TEST_TIMEOUT);
});
