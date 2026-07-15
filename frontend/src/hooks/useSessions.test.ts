/**
 * useSessions hook — BP-14 unit tests.
 *
 * Verifies: polling gates on active+visibility; two-mode switch;
 * auto-selection of first session.
 */

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SessionInfo, SessionView } from "@/api/agent";

// ---- Minimal react-query wrapper ------------------------------------------

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement } from "react";

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return ({ children }: { children: React.ReactNode }) =>
    createElement(QueryClientProvider, { client: qc }, children);
}

// ---- Mocks -----------------------------------------------------------------

const mockGetSessions = vi.fn<(cid: string) => Promise<{ sessions: SessionInfo[] }>>();
const mockGetSessionView = vi.fn<
  (cid: string, name: string, tailChars?: number) => Promise<SessionView>
>();

vi.mock("@/api/agent", () => ({
  getSessions: (cid: string) => mockGetSessions(cid),
  getSessionView: (cid: string, name: string, tailChars?: number) =>
    mockGetSessionView(cid, name, tailChars),
}));

import { useSessions } from "./useSessions";

beforeEach(() => {
  vi.useFakeTimers();
  mockGetSessions.mockResolvedValue({ sessions: [] });
  mockGetSessionView.mockResolvedValue({ name: "dev", busy: true, content: "output" });
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

// ---- Tests -----------------------------------------------------------------

describe("useSessions — polling gates", () => {
  it("does not poll when active=false", async () => {
    renderHook(() => useSessions("cid1", false, false), { wrapper: makeWrapper() });
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(mockGetSessions).not.toHaveBeenCalled();
  });

  it("does not poll when cid=null", async () => {
    renderHook(() => useSessions(null, true, true), { wrapper: makeWrapper() });
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(mockGetSessions).not.toHaveBeenCalled();
  });

  it("polls /sessions when active (even if tab not visible)", async () => {
    renderHook(() => useSessions("cid2", true, false), { wrapper: makeWrapper() });
    await act(() => vi.advanceTimersByTimeAsync(100));
    expect(mockGetSessions).toHaveBeenCalledWith("cid2");
  });

  it("does not poll /view when active but tab not visible", async () => {
    mockGetSessions.mockResolvedValue({
      sessions: [{ name: "dev", busy: true, last_line: "ok" }],
    });
    renderHook(() => useSessions("cid3", true, false), { wrapper: makeWrapper() });
    await act(() => vi.advanceTimersByTimeAsync(3000));
    expect(mockGetSessions).toHaveBeenCalled();
    expect(mockGetSessionView).not.toHaveBeenCalled();
  });

  it("polls /view when active AND visible", async () => {
    mockGetSessions.mockResolvedValue({
      sessions: [{ name: "dev", busy: true, last_line: "ok" }],
    });
    renderHook(() => useSessions("cid4", true, true), { wrapper: makeWrapper() });
    // First advance: triggers sessions initial fetch + microtask resolution
    await act(() => vi.advanceTimersByTimeAsync(200));
    // Second advance: view query fires after sessions data populates resolvedName
    await act(() => vi.advanceTimersByTimeAsync(200));
    expect(mockGetSessionView).toHaveBeenCalled();
  });
});

describe("useSessions — two-mode switch", () => {
  it("returns empty sessions when no sessions exist", async () => {
    mockGetSessions.mockResolvedValue({ sessions: [] });
    const { result } = renderHook(() => useSessions("cid5", true, true), {
      wrapper: makeWrapper(),
    });
    await act(() => vi.advanceTimersByTimeAsync(200));
    expect(result.current.sessions).toEqual([]);
    expect(result.current.selectedName).toBeNull();
  });

  it("auto-selects first session when sessions arrive", async () => {
    mockGetSessions.mockResolvedValue({
      sessions: [
        { name: "dev", busy: true, last_line: "ok" },
        { name: "preview", busy: false, last_line: "serving" },
      ],
    });
    const { result } = renderHook(() => useSessions("cid6", true, true), {
      wrapper: makeWrapper(),
    });
    await act(() => vi.advanceTimersByTimeAsync(200));
    expect(result.current.sessions.length).toBe(2);
    expect(result.current.selectedName).toBe("dev");
  });

  it("respects manual selection when session still exists", async () => {
    mockGetSessions.mockResolvedValue({
      sessions: [
        { name: "dev", busy: true, last_line: "ok" },
        { name: "preview", busy: false, last_line: "serving" },
      ],
    });
    const { result } = renderHook(() => useSessions("cid7", true, true), {
      wrapper: makeWrapper(),
    });
    await act(() => vi.advanceTimersByTimeAsync(200));
    expect(result.current.sessions.length).toBe(2);

    act(() => result.current.setSelectedName("preview"));
    expect(result.current.selectedName).toBe("preview");
  });
});

describe("useSessions — view content", () => {
  it("returns view content from the selected session", async () => {
    mockGetSessions.mockResolvedValue({
      sessions: [{ name: "dev", busy: true, last_line: "ok" }],
    });
    mockGetSessionView.mockResolvedValue({
      name: "dev",
      busy: true,
      content: "live server output",
    });
    const { result } = renderHook(() => useSessions("cid8", true, true), {
      wrapper: makeWrapper(),
    });
    await act(() => vi.advanceTimersByTimeAsync(200));
    await act(() => vi.advanceTimersByTimeAsync(200));
    expect(result.current.view?.content).toBe("live server output");
  });
});
