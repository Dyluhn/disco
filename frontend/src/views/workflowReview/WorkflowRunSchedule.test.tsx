import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { WorkflowReview, WorkflowSchedule } from "@/types/workflow";
import { browserScheduleTimezone } from "@/lib/scheduleLocal";
import { WorkflowRunSchedule } from "./WorkflowRunSchedule";

const wire = vi.hoisted(() => ({ get: vi.fn(), send: vi.fn() }));
vi.mock("@/api/client", async (original) => ({
  ...await original<typeof import("@/api/client")>(),
  agentLive: () => true,
  agentGet: wire.get,
  agentSend: wire.send,
}));
vi.mock("@/components/toastApi", () => ({ useToast: () => ({ show: vi.fn() }) }));

const workflow = { instance_id: "wf_one", definition_digest: "a".repeat(64), name: "Daily report" } as WorkflowReview;
let saved: WorkflowSchedule[];
function row(id: string, instance = "wf_one"): WorkflowSchedule {
  return { schedule_id: id, spec: { instance_id: instance, instance_digest: workflow.definition_digest,
    cron: "* * * * *", timezone: browserScheduleTimezone(), enabled: true }, next_run: "2026-09-25T12:00:00Z" };
}
function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={qc}><MemoryRouter><WorkflowRunSchedule workflow={workflow} /></MemoryRouter></QueryClientProvider>);
}

beforeEach(() => {
  saved = [row("wfsched_owned"), row("wfsched_other", "wf_other")];
  wire.get.mockReset().mockImplementation(async () => ({ schedules: structuredClone(saved) }));
  wire.send.mockReset().mockImplementation(async (method, path, body) => {
    if (method === "PATCH") {
      const match = saved.find((item) => path.endsWith(`/${item.schedule_id}`))!;
      match.spec.enabled = body.enabled;
      match.next_run = body.enabled ? "2026-09-26T12:00:00Z" : null;
      return structuredClone(match);
    }
    const created = row("wfsched_created");
    created.spec.cron = body.cron;
    saved.push(created);
    return structuredClone(created);
  });
});

describe("workflow schedule management", () => {
  it("lists only this workflow, pauses exact schedule, persists on reload, and resumes", async () => {
    const user = userEvent.setup();
    const view = mount();
    const pause = await screen.findByRole("button", { name: "Pause schedule" });
    expect(screen.getAllByRole("button", { name: "Pause schedule" })).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    await user.click(pause);
    expect(await screen.findByText("Paused")).toBeInTheDocument();
    expect(wire.send).toHaveBeenCalledWith("PATCH", "/api/workflows/schedules/wfsched_owned", { enabled: false });
    expect(saved[1].spec.enabled).toBe(true);
    expect(screen.queryByText(/^Next:/)).not.toBeInTheDocument();
    view.unmount();
    mount();
    await user.click(await screen.findByRole("button", { name: "Resume schedule" }));
    expect(await screen.findByText("Active")).toBeInTheDocument();
    expect(wire.send).toHaveBeenLastCalledWith("PATCH", "/api/workflows/schedules/wfsched_owned", { enabled: true });
  });

  it("saves a new cron once and immediately makes the new schedule manageable", async () => {
    const user = userEvent.setup();
    mount();
    await screen.findByText("Active");
    fireEvent.change(screen.getByRole("textbox", { name: "Cron schedule for Daily report" }), { target: { value: "0 8 * * *" } });
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.getAllByRole("button", { name: "Pause schedule" })).toHaveLength(2));
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(wire.send).toHaveBeenCalledTimes(1);
  });

  it("shows update failure without optimistically claiming the schedule is paused", async () => {
    const user = userEvent.setup();
    wire.send.mockRejectedValueOnce(new Error("network unavailable"));
    mount();
    await user.click(await screen.findByRole("button", { name: "Pause schedule" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not confirm schedule update");
    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Pause schedule" })).toBeEnabled();
  });
});
