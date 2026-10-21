import { useState } from "react";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PreviewLaunchFrame } from "./PreviewLaunchFrame";
import type { PreviewLaunch } from "@/api/preview";

const url = "http://isolated.example/bootstrap";
const origin = new URL(url).origin;
function metadata(frame: HTMLIFrameElement, lifetime = 120, eventOrigin = origin, source: MessageEventSource | null = frame.contentWindow) {
  act(() => window.dispatchEvent(new MessageEvent("message", {
    source, origin: eventOrigin,
    data: { channel: "disco-preview-access", expires_at: 1000 + lifetime, server_time: 1000 },
  })));
}
function ready(frame: HTMLIFrameElement) {
  act(() => window.dispatchEvent(new MessageEvent("message", {
    source: frame.contentWindow, data: "disco-preview-bootstrap-ready",
  })));
  fireEvent.load(frame);
}
function draft(frame: HTMLIFrameElement) {
  const input = frame.contentDocument!.createElement("input");
  input.value = "unsaved notes";
  frame.contentDocument!.body.append(input);
  return input;
}
beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-09-25T10:00:00Z"));
  vi.spyOn(HTMLFormElement.prototype, "submit").mockImplementation(() => {});
});
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

describe("Preview access expiry", () => {
  it("warns then blocks the same iframe without losing typed data or relaunching", () => {
    const reload = vi.fn();
    const { getByTitle } = render(<PreviewLaunchFrame title="App" launch={{ url, intent: "one" }} onReload={reload} />);
    const frame = getByTitle("App") as HTMLIFrameElement;
    const input = draft(frame);
    metadata(frame);
    ready(frame);
    expect(screen.queryByTestId("preview-access-notice")).toBeNull();
    act(() => vi.advanceTimersByTime(60_000));
    expect(screen.getByText(/access expires soon/)).toBeInTheDocument();
    expect(frame.parentElement).not.toHaveAttribute("inert");
    act(() => vi.advanceTimersByTime(60_000));
    expect(screen.getByRole("alert")).toHaveTextContent("Preview access expired");
    expect(frame.parentElement).toHaveAttribute("inert");
    expect(getByTitle("App")).toBe(frame);
    expect(input.isConnected).toBe(true);
    expect(input.value).toBe("unsaved notes");
    expect(reload).not.toHaveBeenCalled();
    expect(HTMLFormElement.prototype.submit).toHaveBeenCalledTimes(1);
  });

  it("Cancel preserves the frame and confirmation performs exactly one explicit launch", async () => {
    const mint = vi.fn(async () => ({ url, intent: "two" }));
    function App() {
      const [launch, setLaunch] = useState<PreviewLaunch>({ url, intent: "one", accessDeadline: Date.now() });
      return <PreviewLaunchFrame title="App" launch={launch} onReload={async () => setLaunch(await mint())} />;
    }
    const { getByTitle } = render(<App />);
    const original = getByTitle("App") as HTMLIFrameElement;
    const input = draft(original);
    fireEvent.click(screen.getByRole("button", { name: "Reload Preview" }));
    expect(screen.getByRole("dialog")).toHaveTextContent("Unsaved entries may be lost");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(getByTitle("App")).toBe(original);
    expect(input.value).toBe("unsaved notes");
    expect(mint).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Reload Preview" }));
    await act(async () => fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Reload Preview" })));
    expect(mint).toHaveBeenCalledTimes(1);
    expect(getByTitle("App")).not.toBe(original);
    expect(HTMLFormElement.prototype.submit).toHaveBeenCalledTimes(2);
  });

  it("clamps the verified cookie deadline to the earlier local lease deadline", () => {
    const { getByTitle } = render(<PreviewLaunchFrame title="App" launch={{ url, intent: "one", accessDeadline: Date.now() + 90_000 }} />);
    metadata(getByTitle("App") as HTMLIFrameElement, 900);
    act(() => vi.advanceTimersByTime(90_000));
    expect(screen.getByRole("alert")).toHaveTextContent("Preview access expired");
  });

  it("rejects wrong origins, wrong frames and old-launch metadata", () => {
    const { getByTitle, rerender } = render(<PreviewLaunchFrame title="App" launch={{ url, intent: "one" }} />);
    const first = getByTitle("App") as HTMLIFrameElement;
    const oldWindow = first.contentWindow;
    metadata(first, 900, "http://wrong.example");
    metadata(first, 900, origin, window);
    expect(screen.getByTestId("preview-access-notice")).toHaveTextContent("expiry is unavailable");
    rerender(<PreviewLaunchFrame title="App" launch={{ url, intent: "two" }} />);
    const current = getByTitle("App") as HTMLIFrameElement;
    metadata(current, 900, origin, oldWindow);
    expect(screen.getByTestId("preview-access-notice")).toHaveTextContent("expiry is unavailable");
    metadata(current, 120);
    act(() => vi.advanceTimersByTime(120_000));
    expect(screen.getByRole("alert")).toHaveTextContent("Preview access expired");
  });

  it("latches the first verified deadline and ignores later app attempts to extend it", () => {
    const { getByTitle } = render(<PreviewLaunchFrame title="App" launch={{ url, intent: "one" }} />);
    const frame = getByTitle("App") as HTMLIFrameElement;
    metadata(frame, 90);
    metadata(frame, 90_000);
    ready(frame);
    metadata(frame, 90_000);
    act(() => vi.advanceTimersByTime(90_000));
    expect(screen.getByRole("alert")).toHaveTextContent("Preview access expired");
  });

  it("retains legacy ready handshakes but cannot accept deadline claims from the loaded app", () => {
    const loaded = vi.fn();
    const { getByTitle } = render(<PreviewLaunchFrame title="App" launch={{ url, intent: "one" }} onLoad={loaded} />);
    const frame = getByTitle("App") as HTMLIFrameElement;
    ready(frame);
    metadata(frame, 900);
    expect(loaded).toHaveBeenCalledOnce();
    expect(screen.getByTestId("preview-access-notice")).toHaveTextContent("expiry is unavailable");
  });

  it.each(["focus", "visibilitychange"])("rechecks a suspended timer on %s using elapsed wall time", (event) => {
    const { getByTitle } = render(<PreviewLaunchFrame title="App" launch={{ url, intent: "one" }} />);
    metadata(getByTitle("App") as HTMLIFrameElement, 120);
    vi.setSystemTime(Date.now() + 180_000);
    act(() => (event === "focus" ? window : document).dispatchEvent(new Event(event)));
    expect(screen.getByRole("alert")).toHaveTextContent("Preview access expired");
  });

  it("does not move a new launch deadline when its predecessor's old timer fires", () => {
    const { getByTitle, rerender } = render(<PreviewLaunchFrame title="App" launch={{ url, intent: "one" }} />);
    metadata(getByTitle("App") as HTMLIFrameElement, 90);
    rerender(<PreviewLaunchFrame title="App" launch={{ url, intent: "two" }} />);
    metadata(getByTitle("App") as HTMLIFrameElement, 900);
    act(() => vi.advanceTimersByTime(120_000));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByTestId("preview-access-notice")).toBeNull();
  });

  it("keeps typed data when the explicit reload fails without automatic retries", async () => {
    const reload = vi.fn(async () => { throw new Error("offline"); });
    const { getByTitle } = render(<PreviewLaunchFrame title="App" launch={{ url, intent: "one", accessDeadline: Date.now() }} onReload={reload} />);
    const frame = getByTitle("App") as HTMLIFrameElement;
    const input = draft(frame);
    fireEvent.click(screen.getByRole("button", { name: "Reload Preview" }));
    await act(async () => fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Reload Preview" })));
    expect(screen.getByRole("alert")).toHaveTextContent("could not be reloaded");
    expect(getByTitle("App")).toBe(frame);
    expect(input.value).toBe("unsaved notes");
    act(() => vi.advanceTimersByTime(60_000));
    expect(reload).toHaveBeenCalledTimes(1);
  });
  it("accepts fractional server timestamps and closes metadata before the app load event", () => {
    const { getByTitle } = render(<PreviewLaunchFrame title="App" launch={{ url, intent: "one" }} />);
    const frame = getByTitle("App") as HTMLIFrameElement;
    act(() => window.dispatchEvent(new MessageEvent("message", { source: frame.contentWindow, origin,
      data: { channel: "disco-preview-access", expires_at: 1090, server_time: 1000.75 } })));
    act(() => window.dispatchEvent(new MessageEvent("message", { source: frame.contentWindow,
      data: "disco-preview-bootstrap-ready" })));
    metadata(frame, 9000);
    act(() => vi.advanceTimersByTime(89_250));
    expect(screen.getByRole("alert")).toHaveTextContent("Preview access expired");
    vi.setSystemTime(Date.now() - 600_000);
    act(() => window.dispatchEvent(new Event("focus")));
    expect(frame.parentElement).toHaveAttribute("inert");
  });

  it.each([NaN, Infinity, -1, "1000"])("rejects malformed server timestamp %s", (serverTime) => {
    const { getByTitle } = render(<PreviewLaunchFrame title="App" launch={{ url, intent: "one" }} />);
    const frame = getByTitle("App") as HTMLIFrameElement;
    act(() => window.dispatchEvent(new MessageEvent("message", { source: frame.contentWindow, origin,
      data: { channel: "disco-preview-access", expires_at: 1090, server_time: serverTime } })));
    expect(screen.getByTestId("preview-access-notice")).toHaveTextContent("expiry is unavailable");
    metadata(frame, 90);
    act(() => vi.advanceTimersByTime(90_000));
    expect(frame.parentElement).toHaveAttribute("inert");
  });

});
