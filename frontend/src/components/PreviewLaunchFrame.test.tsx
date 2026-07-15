import { StrictMode } from "react";
import { act, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PreviewLaunchFrame } from "@/components/PreviewLaunchFrame";
import { openFreshPreview } from "@/lib/previewLaunch";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("PreviewLaunchFrame", () => {
  it("submits one body-only form under StrictMode effect replay", async () => {
    const submissions: Array<{
      action: string;
      target: string;
      intent: string | undefined;
    }> = [];
    vi.spyOn(HTMLFormElement.prototype, "submit").mockImplementation(function (this: HTMLFormElement) {
      const input = this.elements.namedItem("intent") as HTMLInputElement | null;
      submissions.push({
        action: this.action,
        target: this.target,
        intent: input?.value,
      });
    });

    render(
      <StrictMode>
        <PreviewLaunchFrame
          title="Strict preview"
          launch={{
            url: "http://isolated.example/__disco/preview-auth",
            intent: "signed-intent",
          }}
        />
      </StrictMode>,
    );

    await waitFor(() => expect(submissions).toHaveLength(1));
    expect(submissions[0]).toMatchObject({
      action: "http://isolated.example/__disco/preview-auth",
      intent: "signed-intent",
    });
    expect(submissions[0]?.target).toMatch(/^disco-preview-frame-/);
    expect(submissions[0]?.action).not.toContain("signed-intent");
    expect(document.querySelector('input[name="intent"]')).toBeNull();
  });

  it("forwards onLoad only after the signed trampoline announces final navigation", async () => {
    vi.spyOn(HTMLFormElement.prototype, "submit").mockImplementation(() => {});
    const onLoad = vi.fn();
    const { getByTitle } = render(
      <PreviewLaunchFrame
        title="Load-gated preview"
        launch={{ url: "http://isolated.example/bootstrap", intent: "intent" }}
        onLoad={onLoad}
      />,
    );
    const frame = getByTitle("Load-gated preview") as HTMLIFrameElement;

    fireEvent.load(frame); // initial about:blank
    fireEvent.load(frame); // POST trampoline
    expect(onLoad).not.toHaveBeenCalled();

    window.dispatchEvent(
      new MessageEvent("message", {
        data: "disco-preview-bootstrap-ready",
        source: frame.contentWindow,
      }),
    );
    fireEvent.load(frame); // exact signed target
    expect(onLoad).toHaveBeenCalledOnce();
    window.dispatchEvent(
      new MessageEvent("message", {
        data: "disco-preview-bootstrap-ready",
        source: frame.contentWindow,
      }),
    );
    fireEvent.load(frame);
    expect(onLoad).toHaveBeenCalledOnce();
  });

  it("replaces the named browsing context and ignores stale launch messages", async () => {
    const formTargets: string[] = [];
    vi.spyOn(HTMLFormElement.prototype, "submit").mockImplementation(function (this: HTMLFormElement) {
      formTargets.push(this.target);
    });
    const onLoad = vi.fn();
    const { getByTitle, rerender } = render(
      <PreviewLaunchFrame
        title="Changing preview"
        launch={{ url: "http://isolated.example/bootstrap", intent: "intent-a" }}
        onLoad={onLoad}
      />,
    );
    const oldFrame = getByTitle("Changing preview") as HTMLIFrameElement;
    const oldWindow = oldFrame.contentWindow;

    rerender(
      <PreviewLaunchFrame
        title="Changing preview"
        launch={{ url: "http://isolated.example/bootstrap", intent: "intent-b" }}
        onLoad={onLoad}
      />,
    );
    const newFrame = getByTitle("Changing preview") as HTMLIFrameElement;
    expect(newFrame).not.toBe(oldFrame);
    await waitFor(() => expect(formTargets).toHaveLength(2));
    expect(formTargets[0]).not.toBe(formTargets[1]);
    expect(oldFrame.name).toBe(formTargets[0]);
    expect(newFrame.name).toBe(formTargets[1]);

    window.dispatchEvent(
      new MessageEvent("message", {
        data: "disco-preview-bootstrap-ready",
        source: oldWindow,
      }),
    );
    fireEvent.load(newFrame);
    expect(onLoad).not.toHaveBeenCalled();

    window.dispatchEvent(
      new MessageEvent("message", {
        data: "disco-preview-bootstrap-ready",
        source: newFrame.contentWindow,
      }),
    );
    fireEvent.load(newFrame);
    expect(onLoad).toHaveBeenCalledOnce();
  });

  it("renders an intentless local preview src declaratively", () => {
    const onLoad = vi.fn();
    const { getByTitle } = render(
      <PreviewLaunchFrame
        title="Local preview"
        launch={{ url: "http://localhost:8000/", intent: "" }}
        onLoad={onLoad}
      />,
    );
    const frame = getByTitle("Local preview") as HTMLIFrameElement;
    expect(frame.src).toBe("http://localhost:8000/");
    fireEvent.load(frame);
    expect(onLoad).toHaveBeenCalledOnce();
  });

  it("opens synchronously and mints a fresh popup launch on every click", async () => {
    const popups = [
      { opener: window, close: vi.fn() },
      { opener: window, close: vi.fn() },
    ];
    vi.spyOn(window, "open")
      .mockReturnValueOnce(popups[0] as unknown as Window)
      .mockReturnValueOnce(popups[1] as unknown as Window);
    const submissions: string[] = [];
    vi.spyOn(HTMLFormElement.prototype, "submit").mockImplementation(function (this: HTMLFormElement) {
      submissions.push((this.elements.namedItem("intent") as HTMLInputElement).value);
    });
    let mintCount = 0;
    const mint = vi.fn(async () => {
      mintCount += 1;
      return {
        url: "http://isolated.example/bootstrap",
        intent: `fresh-${mintCount}`,
      };
    });

    expect(openFreshPreview(mint)).toBe(popups[0]);
    expect(openFreshPreview(mint)).toBe(popups[1]);
    expect(window.open).toHaveBeenCalledTimes(2);
    expect(popups[0].opener).toBeNull();
    expect(popups[1].opener).toBeNull();
    await act(async () => {});
    expect(mint).toHaveBeenCalledTimes(2);
    expect(submissions).toEqual(["fresh-1", "fresh-2"]);
  });

  it("reports blocked and failed popup launches without silent close", async () => {
    const onError = vi.fn();
    vi.spyOn(window, "open").mockReturnValueOnce(null);
    const blockedMint = vi.fn(async () => null);
    expect(openFreshPreview(blockedMint, onError)).toBeNull();
    expect(blockedMint).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledWith("popup_blocked");

    const popup = { opener: window, close: vi.fn() };
    vi.spyOn(window, "open").mockReturnValueOnce(popup as unknown as Window);
    openFreshPreview(async () => {
      throw new Error("mint failed");
    }, onError);
    await act(async () => {});
    expect(popup.close).toHaveBeenCalledOnce();
    expect(onError).toHaveBeenCalledWith("capability_unavailable");
  });

  it("uses a collision-resistant fallback target without Web Crypto", async () => {
    vi.stubGlobal("crypto", undefined);
    const popup = { opener: window, close: vi.fn() };
    const open = vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);
    vi.spyOn(HTMLFormElement.prototype, "submit").mockImplementation(() => {});

    expect(
      openFreshPreview(async () => ({
        url: "http://isolated.example/bootstrap",
        intent: "intent",
      })),
    ).toBe(popup);
    await act(async () => {});
    expect(open.mock.calls[0]?.[1]).toMatch(/^disco-preview-popup-/);
  });
});
