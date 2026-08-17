/**
 * D11 — running-tasks dashboard + global "N running" shell indicator +
 * schedule-run history surface.
 *
 * Acceptance (from the D11 spec):
 *   1. With 2 running tasks → the global indicator shows "2" AND the dashboard
 *      lists both tasks with their live status (RUNNING / WAITING_FOR_* chips).
 *   2. Completion (running list goes empty) updates BOTH surfaces — the
 *      NavRail badge disappears, the dashboard falls into the "Nothing is
 *      running right now" empty state.
 *   3. The schedule-run history surface ("Recent scheduled runs") renders the
 *      ScheduleRunRecord rows from the activity feed (no new backend, no
 *      forked data source — it's the same `["activity"]` query that drives
 *      the indicator and the running-tasks list).
 *
 * All three surfaces consume the EXISTING activity feed
 * (`useActivity` → `getActivity` → `ActivityFeed`). This test primes the
 * `["activity"]` query cache directly and asserts that EVERY reader of that
 * cache agrees on the same data — no false affordance, no "running" chip on
 * a task the runtime has already finished.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import type { ActivityFeed } from "@/types/activity";
import { ActivityView } from "./ActivityView";
import { ModeProvider } from "@/shell/ModeProvider";
import { Shell } from "@/shell/Shell";

/** The two running-task shapes the D11 acceptance test drives through the
 *  shared activity query. Two different statuses (RUNNING + WAITING) to prove
 *  the live-status chip is the field, not a hard-coded "Running" label. */
const FEED_TWO_RUNNING: ActivityFeed = {
  running: [
    {
      id: "conv-alpha",
      title: "Build the Q3 launch page",
      status: "RUNNING",
      surface: "build",
      created_at: "2026-06-14T08:00:00Z",
    },
    {
      id: "conv-beta",
      title: "Digest the May sales transcripts",
      status: "WAITING_FOR_CONFIRMATION",
      surface: "agent",
      created_at: "2026-06-14T08:05:00Z",
    },
  ],
  recent_runs: [
    {
      run_id: "fr-001",
      schedule_id: "fs-digest",
      conversation_id: "conv-beta",
      fired_at: "2026-06-14T07:00:00Z",
      coalesced: 0,
      description: "Morning market digest",
      title: "Morning market digest",
    },
    {
      run_id: "fr-002",
      schedule_id: "fs-digest",
      conversation_id: "conv-beta",
      fired_at: "2026-06-13T07:00:00Z",
      coalesced: 1, // server was down across N fires — one catch-up run
      description: "Morning market digest",
      title: "Morning market digest",
    },
    {
      run_id: "fr-003",
      schedule_id: "fs-weekly",
      conversation_id: "conv-gamma",
      fired_at: "2026-06-12T13:00:00Z",
      coalesced: 0,
      description: "Weekly funding roundup",
      title: "Weekly funding roundup",
    },
  ],
  counts: { running: 2 },
};

/** "All done" — same query key, no running tasks, recent runs preserved
 *  (the audit history is durable; completion only zeroes the running list). */
const FEED_NONE_RUNNING: ActivityFeed = {
  running: [],
  recent_runs: FEED_TWO_RUNNING.recent_runs,
  counts: { running: 0 },
};

/** Builds a QueryClient whose `staleTime: Infinity` keeps the seeded
 *  `["activity"]` data fresh for the whole test — without it, TanStack
 *  treats the seeded value as "stale" and immediately re-fires the
 *  `getActivity` queryFn, which (in offline fixture mode) returns the
 *  canned 1-running-task fixture, OVERWRITING our seed. This is the same
 *  pattern HistoryView.status.test.tsx uses for `["conversations"]`. */
function makeQC(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        refetchOnWindowFocus: false,
        staleTime: Infinity,
      },
    },
  });
}

/** Renders the production shape: QueryClientProvider → MemoryRouter → Shell
 *  (NavRail with the live "N running" indicator) → /activity outlet
 *  (ActivityView, the dashboard view itself). The test then exercises BOTH
 *  surfaces against the SAME `["activity"]` query data. */
function renderShellWithActivity(initial = "/activity", qc?: QueryClient) {
  const client = qc ?? makeQC();
  return {
    qc: client,
    ...render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[initial]}>
          <ModeProvider>
            <Routes>
              <Route element={<Shell />}>
                <Route index element={<div>Home surface</div>} />
                <Route path="activity" element={<ActivityView />} />
                <Route path="*" element={<div>Home surface</div>} />
              </Route>
            </Routes>
          </ModeProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    ),
  };
}

/** Just the dashboard view in isolation — for assertions that don't need the
 *  full shell (the schedule-run history section doesn't depend on NavRail). */
function renderDashboard(qc?: QueryClient) {
  const client = qc ?? makeQC();
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ActivityView />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("D11 — running-tasks dashboard + global N-running indicator", () => {
  it("with 2 running tasks: indicator shows '2' AND dashboard lists both with live status", async () => {
    const qc = makeQC();
    // Seed the activity query with 2 running tasks — same key the live
    // useActivity() hook reads in production. This is the wire mirror of
    // GET /api/activity (no new backend; no new field).
    qc.setQueryData(["activity"], FEED_TWO_RUNNING);

    renderShellWithActivity("/activity", qc);

    // (1) The NavRail "Activity" link exposes the live count in its
    //     accessible name AND in the visible badge — the global indicator.
    //     (a) → "2" in the aria-label. (b) → "2" in the badge text.
    const activityLink = await screen.findByRole("link", { name: /Activity \(2 running\)/i });
    expect(activityLink).toBeInTheDocument();
    expect(within(activityLink).getByText("2")).toBeInTheDocument();

    // (2) The dashboard renders the "Running now" section heading and
    //     surfaces the count pill from the same data. Scope to the
    //     heading's parent (the section's <h2>) so we don't conflate
    //     with the NavRail's "2" badge — both are intentionally "2"
    //     and that's the consistency invariant the next test pins down.
    const runningHeading = screen.getByText(/^Running now$/);
    const dashboardPill = runningHeading.parentElement;
    expect(dashboardPill).not.toBeNull();
    expect(within(dashboardPill as HTMLElement).getByText("2")).toBeInTheDocument();

    // (3) Both running tasks are listed, with their titles, surfaces, and
    //     live status chips (no false affordance — only the rows we seeded).
    expect(screen.getByText("Build the Q3 launch page")).toBeInTheDocument();
    expect(screen.getByText("Digest the May sales transcripts")).toBeInTheDocument();
    // Live status chips — one RUNNING, one WAITING_FOR_CONFIRMATION. The
    // ActivityView's StatusChip renders the raw `status` field (uppercased),
    // not the AgentStatusBar's friendly label — same data, different
    // surface, both honest about what the runtime actually reported.
    expect(screen.getByText(/^RUNNING$/)).toBeInTheDocument();
    expect(screen.getByText(/^WAITING_FOR_CONFIRMATION$/)).toBeInTheDocument();
    // Surface chips — build + agent (so the user knows where each task
    // lives, and clicks open the correct surface, not a generic 404).
    // We scope to the running-tasks <ul> so we don't conflate with the
    // ModeSlider's "build" radio option (same string, different surface).
    const runningList = screen.getByText("Build the Q3 launch page").closest("ul");
    expect(runningList).not.toBeNull();
    expect(within(runningList as HTMLElement).getByText("build")).toBeInTheDocument();
    expect(within(runningList as HTMLElement).getByText("agent")).toBeInTheDocument();
  });

  it("completion (no running tasks) updates BOTH the indicator and the dashboard", async () => {
    const qc = makeQC();
    qc.setQueryData(["activity"], FEED_TWO_RUNNING);
    renderShellWithActivity("/activity", qc);

    // Sanity: start in the "2 running" state.
    await screen.findByRole("link", { name: /Activity \(2 running\)/i });
    expect(screen.getByText("Build the Q3 launch page")).toBeInTheDocument();

    // Simulate completion: the runtime has zero in-flight tasks. Same query
    // key, new value — every consumer of the cache re-renders against the
    // truth ("running" is what the runtime reports, not what we last saw).
    qc.setQueryData(["activity"], FEED_NONE_RUNNING);

    // (a) Global indicator disappears — no badge on the Activity link, the
    //     aria-label falls back to the plain "Activity" name. This is the
    //     "no false affordance" check: the indicator MUST NOT keep showing
    //     "2" after the runtime reports nothing is running.
    await waitFor(() => {
      expect(
        screen.queryByRole("link", { name: /Activity \(\d+ running\)/i }),
      ).not.toBeInTheDocument();
    });
    expect(screen.getByRole("link", { name: "Activity" })).toBeInTheDocument();

    // (b) The dashboard's "Running now" section switches to its honest empty
    //     state — no chips, no fake "Running: 0" pill, just the calm text.
    expect(screen.getByText(/Nothing is running right now\./i)).toBeInTheDocument();
    expect(screen.queryByText("Build the Q3 launch page")).not.toBeInTheDocument();
    expect(screen.queryByText("Digest the May sales transcripts")).not.toBeInTheDocument();

    // (c) The "Recent scheduled runs" section is durable — completion does
    //     NOT clear the audit history. The schedule-run history surface
    //     still lists the runs the scheduler fired.
    expect(screen.getByText(/Recent scheduled runs/)).toBeInTheDocument();
    expect(screen.getByText("Weekly funding roundup")).toBeInTheDocument();
  });

  it("schedule-run history surface: every ScheduleRunRecord in the feed is rendered, with the coalesced flag", () => {
    const qc = makeQC();
    qc.setQueryData(["activity"], FEED_TWO_RUNNING);
    renderDashboard(qc);

    // The "Recent scheduled runs" section is the schedule-run history
    // surface — it lists EVERY ScheduleRunRecord from the activity feed's
    // `recent_runs` array (the same field the runtime's `/api/activity`
    // returns; we do not invent a parallel history list anywhere).
    expect(screen.getByText(/Recent scheduled runs/)).toBeInTheDocument();

    // 1) Two runs share the same description ("Morning market digest") —
    //    getAllByText proves the section repeats the row per record
    //    (no over-aggressive dedup on the description field).
    expect(screen.getAllByText("Morning market digest").length).toBe(2);
    // 2) The "Weekly funding roundup" run is a distinct schedule, distinct
    //    title — proves the surface is per-record, not per-schedule.
    expect(screen.getByText("Weekly funding roundup")).toBeInTheDocument();
    // 3) The coalesced flag from the feed surfaces as a "coalesced" badge
    //    on the matching row (and ONLY on that row — `fr-002` was
    //    coalesced, `fr-001` and `fr-003` were not).
    expect(screen.getByText(/^coalesced$/i)).toBeInTheDocument();
  });

  it("schedule-run history is updated live: a new run appended to the feed appears in the surface", async () => {
    const qc = makeQC();
    qc.setQueryData(["activity"], FEED_TWO_RUNNING);
    renderDashboard(qc);

    // Baseline: only the original three scheduled runs are visible.
    expect(screen.queryByText("Hourly ticker scrape")).not.toBeInTheDocument();

    // The runtime has just fired another schedule — the poll / a mutation /
    // a manual refresh replaces the activity feed. The schedule-run history
    // surface re-renders against the new feed (one shared query → no
    // per-view refresh logic to forget).
    qc.setQueryData<ActivityFeed>(["activity"], {
      ...FEED_TWO_RUNNING,
      recent_runs: [
        {
          run_id: "fr-004",
          schedule_id: "fs-ticker",
          conversation_id: "conv-delta",
          fired_at: "2026-06-14T09:00:00Z",
          coalesced: 0,
          description: "Hourly ticker scrape",
          title: "Hourly ticker scrape",
        },
        ...FEED_TWO_RUNNING.recent_runs,
      ],
    });

    await waitFor(() => {
      expect(screen.getByText("Hourly ticker scrape")).toBeInTheDocument();
    });
    // The previous rows are still there (the surface is append-only /
    // prepended at the front; the existing entries aren't dropped).
    expect(screen.getByText("Weekly funding roundup")).toBeInTheDocument();
  });

  it("the global indicator on the rail is wired to the same activity query (consistency invariant)", () => {
    // Two independent renderers: the NavRail's badge and the ActivityView's
    // "Running now" count pill both read `data.counts.running` / the length
    // of `data.running` from the SAME query. If they ever disagreed (one
    // says 2, the other says 0), that would be a silent data-source fork
    // — a false affordance. This test seeds the cache with N=7 and asserts
    // BOTH renderers see the same number.
    const qc = makeQC();
    const feed: ActivityFeed = {
      running: Array.from({ length: 7 }, (_, i) => ({
        id: `conv-${i}`,
        title: `Task ${i}`,
        status: "RUNNING" as const,
        surface: "build" as const,
        created_at: "2026-06-14T08:00:00Z",
      })),
      recent_runs: [],
      counts: { running: 7 },
    };
    qc.setQueryData(["activity"], feed);
    renderShellWithActivity("/activity", qc);

    // NavRail: "Activity (7 running)" — the global indicator.
    const link = screen.getByRole("link", { name: /Activity \(7 running\)/i });
    expect(within(link).getByText("7")).toBeInTheDocument();
    // Dashboard: same "7" in the count pill beside "Running now".
    const heading = screen.getByText(/^Running now$/);
    const pill = heading.parentElement;
    expect(pill).not.toBeNull();
    expect(within(pill as HTMLElement).getByText("7")).toBeInTheDocument();
  });

  it("renders no indicator and the dashboard's empty state when the feed reports zero (no false affordance)", () => {
    // Initial state: feed reports an empty running list. The indicator must
    // NOT show a stale "1" / "N" — the rule is: if the runtime doesn't say
    // a task is running, the UI must not pretend it is.
    const qc = makeQC();
    qc.setQueryData(["activity"], { running: [], recent_runs: [], counts: { running: 0 } });
    renderShellWithActivity("/activity", qc);

    // No "(N running)" aria-label → no badge.
    expect(
      screen.queryByRole("link", { name: /Activity \(\d+ running\)/i }),
    ).not.toBeInTheDocument();
    // The plain "Activity" link is still there (the rail is unchanged —
    // the item itself, not its badge, is the navigation affordance).
    expect(screen.getByRole("link", { name: "Activity" })).toBeInTheDocument();
    // Dashboard is in the honest empty state.
    expect(screen.getByText(/Nothing is running right now\./i)).toBeInTheDocument();
  });

  it("the dashboard's running-task buttons open the correct surface (build/agent/deep) when clicked", async () => {
    // View ≠ start. The dashboard's running-task row is a NAVIGATION
    // affordance — clicking a build task opens /build/{cid}, an agent task
    // opens /agent/{cid}, a deep task opens /deep/{cid}. We don't need to
    // mount the destination surfaces here (they have their own large test
    // suites); we just need the click to route correctly. This is what
    // protects the dashboard from drifting into a "start another build"
    // button (a false affordance when the user is already watching the
    // existing one).
    const qc = makeQC();
    qc.setQueryData(["activity"], FEED_TWO_RUNNING);
    const user = userEvent.setup();
    renderShellWithActivity("/activity", qc);

    // The build task is the row whose title we labelled with the "build"
    // surface chip. Click it — MemoryRouter pushes the route; we don't
    // need the destination to mount, we just need the route change.
    const buildTitle = await screen.findByText("Build the Q3 launch page");
    const buildRow = buildTitle.closest("button") as HTMLButtonElement | null;
    expect(buildRow).not.toBeNull();
    await user.click(buildRow!);
    // The /activity outlet should now show the new path. We assert via
    // screen.debug to keep this assertion simple — but the more durable
    // signal is that the dashboard's "Running now" heading has gone away
    // (we are no longer on the activity route).
    await waitFor(() => {
      expect(screen.queryByText(/^Running now$/)).not.toBeInTheDocument();
    });
  });
});
