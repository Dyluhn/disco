import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { agentFetch } from "@/api/client";
import { browserScheduleTimezone } from "@/lib/scheduleLocal";
import { ScheduleSection } from "./ScheduleSection";

vi.mock("@/api/client", () => ({ agentFetch: vi.fn() }));

function response(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
  } as Response;
}

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
    vi.mocked(agentFetch).mockImplementation(async (url, init) => {
      const method = init?.method ?? "GET";
      if (method === "GET") return response({ schedules: [] });
      const body = JSON.parse(String(init?.body ?? "{}"));
      if (url === "/api/schedules/preview") {
        return response({
          rrule: body.rrule,
          timezone: body.timezone,
          next_runs: ["2026-07-13T09:00:00-05:00"],
        });
      }
      return response({ schedule_id: "sched_local", ...body });
    });
  });

  it("preserves a preset's human label and browser timezone through preview and save", async () => {
    const timezone = browserScheduleTimezone();
    renderSection();
    await screen.findByText("No schedules yet. Create one to automate recurring runs.");
    await userEvent.click(screen.getByRole("button", { name: "New schedule" }));
    await userEvent.click(screen.getByRole("button", { name: "Weekly Mon 9am" }));

    expect(await screen.findByText("every Monday at 9:00 AM")).toBeInTheDocument();
    expect(screen.getByText(timezone)).toBeInTheDocument();

    const previewCall = vi.mocked(agentFetch).mock.calls.find(
      ([url]) => url === "/api/schedules/preview",
    );
    expect(JSON.parse(String(previewCall?.[1]?.body))).toEqual({
      rrule: "0 9 * * 1",
      timezone,
      n: 3,
    });

    await userEvent.click(screen.getByRole("button", { name: "Save schedule" }));
    await waitFor(() => {
      const createCall = vi.mocked(agentFetch).mock.calls.find(
        ([url, init]) => url.includes("/conversations/") && init?.method === "POST",
      );
      expect(JSON.parse(String(createCall?.[1]?.body))).toEqual({
        rrule: "0 9 * * 1",
        description: "every Monday at 9:00 AM",
        timezone,
      });
    });
  });
});
