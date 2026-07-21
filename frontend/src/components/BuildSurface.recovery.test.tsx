/**
 * BuildSurface — errored-session RECOVERY + clean title on resume.
 *
 * Two design failures fixed here, both exercised with a MOCKED useBuild so we can
 * drive an exact ERROR status / resumed session (the App-fixture integration test
 * can't reach these paths):
 *
 *  - RECOVERY: when a build errors, the ErrorState's "Try again" button must RESUME
 *    the conversation (re-kick from persisted history — no lost progress), NOT call
 *    reset (which discards the session). onRetry is wired to b.resume.
 *
 *  - CLEAN TITLE ON RESUME: re-entering a session seeds task="(resumed)"; the H1 must
 *    prefer the STORED auto-title (fetched from /state) over the raw first prompt, and
 *    when no stored title exists, fall back to a TRUNCATED first task — never the giant
 *    wall of text the user originally typed.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentEvent, ConversationStatus } from "@/types/agent";

let buildState: Record<string, unknown>;
const { pathPreviewBootstrapUrlMock, showToastMock } = vi.hoisted(() => ({
  pathPreviewBootstrapUrlMock: vi.fn(),
  showToastMock: vi.fn(),
}));

vi.mock("@/hooks/useBuild", () => ({
  useBuild: () => buildState,
}));
vi.mock("@/hooks/useProjects", () => ({
  useDownloadProject: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useExportManifest: () => ({ mutate: vi.fn() }),
  useProjectManifest: () => ({ data: { files: [] } }),
  useProjectRelease: () => ({ data: undefined }),
}));
// agentLive() TRUE so the surface's /state fetch (which carries the stored title)
// actually runs; agentHttpBase() empty so the URL is relative and intercepted by
// the stubbed global.fetch below.
vi.mock("@/api/client", async (orig) => ({
  ...(await orig<typeof import("@/api/client")>()),
  agentLive: () => true,
  agentHttpBase: () => "",
  pathPreviewBootstrapUrl: pathPreviewBootstrapUrlMock,
}));
vi.mock("@/components/toastApi", () => ({
  useToast: () => ({ show: showToastMock }),
}));

import { BuildSurface } from "@/components/BuildSurface";

function baseBuild(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    cid: "cid-1",
    status: "RUNNING" as ConversationStatus,
    task: "a task",
    events: [] as AgentEvent[],
    pendingActionId: null,
    streamingFile: null,
    started: true,
    modelId: null,
    setModelId: vi.fn(),
    assistChoice: false,
    setAssistChoice: vi.fn(),
    autonomousChoice: false,
    setAutonomousChoice: vi.fn(),
    preCid: null,
    submit: vi.fn(),
    submitting: false,
    submitError: null,
    plan: null,
    awaitingPlan: false,
    buildProgress: new Map(),
    approvePlan: vi.fn(),
    requestPlan: vi.fn(),
    pendingAction: null,
    confirm: vi.fn(),
    reject: vi.fn(),
    awaitingDecision: false,
    pendingAlternatives: null,
    pickAlternative: vi.fn(),
    awaitingQuestion: false,
    pendingClarify: null,
    answer: vi.fn(),
    pendingQuestion: null,
    steer: vi.fn(),
    canResume: false,
    resume: vi.fn(),
    kill: vi.fn(),
    cancel: vi.fn(),
    sandboxState: null,
    autonomous: false,
    assist: false,
    resumed: true,
    reset: vi.fn(),
    error: null,
    ...overrides,
  };
}

function userMessage(id: string, content: string): AgentEvent {
  return {
    kind: "message",
    id,
    source: "user",
    message: { role: "user", content },
  } as AgentEvent;
}

function renderSurface(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

/** Stub the /state fetch the surface fires on cid. `title` is what the backend
 *  overlay returns (the stored auto-title, or null until the titler lands). */
function stubStateFetch(title: string | null) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      json: async () => ({ sandbox_backend: null, title }),
    })) as unknown as typeof fetch,
  );
}

beforeEach(() => {
  stubStateFetch(null);
  pathPreviewBootstrapUrlMock.mockResolvedValue(
    {
      url: "http://localhost:18250/__disco/path-preview-auth/deadbeef",
      intent: "signed-body-intent",
    },
  );
});

afterEach(() => {
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe("RECOVERY: errored build — 'Try again' resumes (never resets)", () => {
  it("wires the ErrorState onRetry to resume, preserving the session", async () => {
    const resume = vi.fn();
    const reset = vi.fn();
    buildState = baseBuild({
      status: "ERROR",
      error: "preflight timed out",
      events: [userMessage("u1", "build a mac + GTA landing page")],
      resume,
      reset,
    });
    const user = userEvent.setup();
    renderSurface(<BuildSurface resumeCid="cid-1" />);

    // the provider's real error is shown verbatim
    expect(screen.getByText(/preflight timed out/i)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /try again/i }));

    // the primary error action RESUMES (re-kick from history) — it does NOT reset
    expect(resume).toHaveBeenCalledTimes(1);
    expect(reset).not.toHaveBeenCalled();
  });

  it("exposes the model picker in the ERROR state so the user can switch before retrying", () => {
    const setModelId = vi.fn();
    buildState = baseBuild({
      status: "ERROR",
      error: "the driver kept dropping the tool call",
      events: [userMessage("u1", "build a landing page")],
      modelId: null,
      setModelId,
    });
    renderSurface(<BuildSurface resumeCid="cid-1" />);

    // The errored UI now offers a model swap next to "Try again" — without this the
    // user could only retry on the SAME (failed) model.
    expect(
      screen.getByLabelText(/choose the model that runs the agent/i),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();
  });
});

describe("finished app handoff isolation", () => {
  const appEvents = [
    userMessage("u1", "build an app"),
    {
      kind: "deliverable",
      id: "d1",
      title: "Finished app",
      path: "release/index.html",
      artifact_kind: "app",
      source: "agent",
    } as AgentEvent,
  ];

  it("opens a blank window synchronously and mints only after the click", async () => {
    const popup = { opener: window, close: vi.fn() };
    const open = vi.fn(() => popup as unknown as Window);
    vi.stubGlobal("open", open);
    const submissions: Array<{ action: string; intent: string }> = [];
    vi.spyOn(HTMLFormElement.prototype, "submit").mockImplementation(function (this: HTMLFormElement) {
      submissions.push({
        action: this.action,
        intent: (this.elements.namedItem("intent") as HTMLInputElement).value,
      });
    });
    buildState = baseBuild({ status: "FINISHED", events: appEvents });
    renderSurface(<BuildSurface resumeCid="cid-1" />);

    const button = await screen.findByRole("button", { name: /open the deliverable/i });
    expect(pathPreviewBootstrapUrlMock).not.toHaveBeenCalled();
    await userEvent.click(button);
    expect(pathPreviewBootstrapUrlMock).toHaveBeenCalledWith("cid-1", "/");
    expect(open).toHaveBeenCalledWith("about:blank", expect.stringMatching(/^disco-preview-popup-/));
    expect(popup.opener).toBeNull();
    await waitFor(() =>
      expect(submissions).toEqual([
        {
          action: "http://localhost:18250/__disco/path-preview-auth/deadbeef",
          intent: "signed-body-intent",
        },
      ]),
    );
  });

  it("closes the blank popup when an isolated handoff cannot be minted", async () => {
    pathPreviewBootstrapUrlMock.mockRejectedValue(new Error("capability unavailable"));
    const popup = { opener: window, close: vi.fn() };
    vi.stubGlobal("open", vi.fn(() => popup as unknown as Window));
    buildState = baseBuild({ status: "FINISHED", events: appEvents });
    renderSurface(<BuildSurface resumeCid="cid-1" />);

    const button = await screen.findByRole("button", { name: /open the deliverable/i });
    expect(pathPreviewBootstrapUrlMock).not.toHaveBeenCalled();
    await userEvent.click(button);
    await waitFor(() => expect(pathPreviewBootstrapUrlMock).toHaveBeenCalled());
    await waitFor(() => expect(popup.close).toHaveBeenCalled());
    expect(showToastMock).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Couldn’t open preview" }),
    );
    expect(button).toBeEnabled();
    expect(document.body.innerHTML).not.toContain("/conversations/cid-1/preview-app/");
  });
});

describe("CLEAN TITLE ON RESUME: the H1 is never the giant raw prompt", () => {
  const WALL =
    "Build me a full marketing site for a macOS productivity app inspired by GTA, " +
    "with a hero, pricing tiers, testimonials, an FAQ, a newsletter signup, and a " +
    "dark theme, plus a downloadable press kit and analytics wired up end to end.";

  it("prefers the STORED auto-title over the raw first prompt on resume", async () => {
    stubStateFetch("macOS + GTA landing site");
    buildState = baseBuild({
      task: "(resumed)",
      events: [userMessage("u1", WALL)],
    });
    renderSurface(<BuildSurface resumeCid="cid-1" />);

    await waitFor(() =>
      expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(
        "macOS + GTA landing site",
      ),
    );
    // the giant wall is NOT the heading
    expect(screen.getByRole("heading", { level: 1 }).textContent).not.toBe(WALL);
  });

  it("falls back to a TRUNCATED first task when no stored title exists", async () => {
    stubStateFetch(null); // titler hasn't landed yet
    buildState = baseBuild({
      task: "(resumed)",
      events: [userMessage("u1", WALL)],
    });
    renderSurface(<BuildSurface resumeCid="cid-1" />);

    const h1 = await screen.findByRole("heading", { level: 1 });
    // never the full wall — capped (~80 chars + ellipsis)
    expect(h1.textContent).not.toBe(WALL);
    expect(h1.textContent!.endsWith("…")).toBe(true);
    expect(h1.textContent!.length).toBeLessThanOrEqual(82);
    expect(WALL.startsWith(h1.textContent!.replace(/…$/, "").trimEnd())).toBe(true);
  });

  it("leaves a FRESH (non-resumed) run's task untouched", async () => {
    buildState = baseBuild({ task: "ship the landing page", events: [] });
    renderSurface(<BuildSurface />);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(
      "ship the landing page",
    );
  });
});
