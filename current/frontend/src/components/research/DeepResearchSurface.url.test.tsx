/**
 * The URL has to carry the run.
 *
 * A run started from `/` left the address bar at `/` while the engine worked,
 * so any reload dropped the live run view back to the composer with the run
 * still going. F1's e2e-live rerun died exactly this way: a vite HMR update
 * forced a full page reload mid-run and Playwright's snapshot at the deadline
 * was the composer while the server was still calling the model.
 *
 * Two things have to be true, and they pull against each other: the run's id
 * must reach the URL, and it must get there by REPLACING the entry once — a
 * push would put the composer behind a Back button during a live run, and a
 * surface already opened at `/deep/:cid` (History, a reload, the resume path)
 * must not navigate at all or every replay would add an entry for a run that
 * is already at its own address.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";
import { FIXTURE_DEEP_CID } from "@/api/deepResearch";
import { ModeProvider } from "@/shell/ModeProvider";
import { DeepResearchSurface } from "./DeepResearchSurface";

/** Every path the router settled on, in order. */
function PathLog({ log }: { log: string[] }) {
  const { pathname } = useLocation();
  useEffect(() => {
    if (log[log.length - 1] !== pathname) log.push(pathname);
  }, [log, pathname]);
  return null;
}

/** What `/` was handed to open with — the real surface reads the same field. */
function SeedProbe() {
  const seed = (useLocation().state as { seedQuery?: string } | null)?.seedQuery;
  return <div data-seed-query={seed ?? ""} />;
}

function mount(entry: string, log: string[]) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[entry]}>
        <ModeProvider>
          <PathLog log={log} />
          <SeedProbe />
          <Routes>
            <Route path="/" element={<DeepResearchSurface />} />
            <Route
              path="/deep/:cid"
              element={<DeepResearchSurface resumeCid={FIXTURE_DEEP_CID} />}
            />
          </Routes>
        </ModeProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("the run's id reaches the URL", () => {
  beforeEach(() => window.localStorage.clear());

  it("moves to /deep/:cid once when a run starts from the composer", async () => {
    const user = userEvent.setup();
    const log: string[] = [];
    mount("/", log);

    await user.type(
      screen.getByPlaceholderText(/ask a research question/i),
      "what is the current state of solid-state battery commercialization?",
    );
    await user.keyboard("{Enter}");

    await waitFor(() => expect(log).toEqual(["/", `/deep/${FIXTURE_DEEP_CID}`]));
    // The run view is what is on screen at the new URL — the navigation
    // re-entered the run, it did not drop it.
    await waitFor(() => expect(document.querySelector("[data-dr-brief]")).not.toBeNull());
    // Still exactly one navigation after the run has produced output.
    expect(log).toEqual(["/", `/deep/${FIXTURE_DEEP_CID}`]);
  });

  it("never navigates on a replay of a run already at its own URL", async () => {
    const log: string[] = [];
    mount(`/deep/${FIXTURE_DEEP_CID}`, log);

    // The surface opens straight into the run view (the composer is gone), so
    // the id it would navigate to is known — and it still must not navigate.
    await screen.findByRole("button", { name: /Standard Search/i });
    expect(screen.queryByPlaceholderText(/ask a research question/i)).toBeNull();
    expect(log).toEqual([`/deep/${FIXTURE_DEEP_CID}`]);
  });

  it("carries the question back to the main surface when the run is left", async () => {
    // The scope handler that used to flip the composer in place belongs to a
    // parent this route does not have, so leaving has to hand the question over
    // instead — `ResearchSurface` seeds its draft from this.
    const user = userEvent.setup();
    const log: string[] = [];
    mount(`/deep/${FIXTURE_DEEP_CID}`, log);

    await user.click(await screen.findByRole("button", { name: /Standard Search/i }));

    await waitFor(() => expect(log).toEqual([`/deep/${FIXTURE_DEEP_CID}`, "/"]));
    expect(
      document.querySelector("[data-seed-query]")?.getAttribute("data-seed-query"),
    ).not.toBe("");
  });
});
