import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  createSchedule,
  listSchedules,
  previewSchedule,
} from "@/api/schedules";
import { browserScheduleTimezone } from "@/lib/scheduleLocal";
import { ScheduleSection } from "./ScheduleSection";

// Amendment A3: components/ may not import `@/api/client` directly.
// ScheduleSection now calls the `@/api/schedules` api module instead of raw
// `agentFetch` — mock that module directly. This controls exactly the same
// wire contract (rrule, timezone, n, description) the previous
// `vi.mock("@/api/client", () => ({ agentFetch: vi.fn() }))` intercepted at
// the `agentFetch` layer.
vi.mock("@/api/schedules", () => ({
  listSchedules: vi.fn(),
  createSchedule: vi.fn(),
  deleteSchedule: vi.fn(),
  previewSchedule: vi.fn(),
}));

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ScheduleSection conversationId="conv_local_time" />
    </QueryClientProvider>,
  );
}

describe("ScheduleSection local-time contract", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listSchedules).mockResolvedValue([]);
    vi.mocked(previewSchedule).mockImplementation(async (rrule, timezone) => ({
      rrule,
      timezone,
      next_runs: ["2026-07-13T09:00:00-05:00"],
    }));
    vi.mocked(createSchedule).mockImplementation(async (conversationId, payload) => ({
      schedule_id: "sched_local",
      conversation_id: conversationId,
      rrule: payload.rrule,
      description: payload.description,
      timezone: payload.timezone,
      depth: payload.depth ?? null,
      model_override: payload.model_override ?? null,
      created_at: "2026-07-13T00:00:00Z",
      enabled: true,
      next_run: null,
    }));
  });

  it("preserves a preset's human label and browser timezone through preview and save", async () => {
    const timezone = browserScheduleTimezone();
    renderSection();
    await screen.findByText("No schedules yet. Create one to automate recurring runs.");
    await userEvent.click(screen.getByRole("button", { name: "New schedule" }));
    await userEvent.click(screen.getByRole("button", { name: "Weekly Mon 9am" }));

    expect(await screen.findByText("every Monday at 9:00 AM")).toBeInTheDocument();
    expect(screen.getByText(timezone)).toBeInTheDocument();

    // The preview call MUST carry the exact rrule/timezone/n — same assertion
    // the previous test made on the serialized fetch body.
    expect(previewSchedule).toHaveBeenCalledWith("0 9 * * 1", timezone, 3);

    await userEvent.click(screen.getByRole("button", { name: "Save schedule" }));
    await waitFor(() => {
      // The create call MUST carry the exact rrule/description/timezone — same
      // assertion the previous test made on the serialized fetch body.
      expect(createSchedule).toHaveBeenCalledWith("conv_local_time", {
        rrule: "0 9 * * 1",
        description: "every Monday at 9:00 AM",
        timezone,
      });
    });
  });
});
