/**
 * Tabbed-away attention: the build status hook badges document.title (and fires an
 * OS Notification when granted) on a meaningful transition WHILE the tab is hidden,
 * and restores the title when the tab refocuses. Visible tab → no noise.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import type { ConversationStatus } from "@/types/agent";
import { useBuildNotifications } from "./useBuildNotifications";

const BASE = "disco";

function setHidden(hidden: boolean) {
  Object.defineProperty(document, "hidden", { value: hidden, configurable: true });
}

describe("useBuildNotifications", () => {
  beforeEach(() => {
    document.title = BASE;
    setHidden(true); // default: user has tabbed away
  });
  afterEach(() => {
    setHidden(false);
    document.title = BASE;
    vi.unstubAllGlobals();
  });

  it("badges the title when a hidden tab's build finishes", () => {
    const { rerender } = renderHook(
      ({ s }: { s: ConversationStatus }) => useBuildNotifications(s, "make fizzbuzz"),
      { initialProps: { s: "RUNNING" as ConversationStatus } },
    );
    rerender({ s: "FINISHED" });
    expect(document.title).toMatch(/Build finished/);
    expect(document.title).toContain(BASE);
  });

  it("badges 'Needs your input' when a gate opens on a hidden tab", () => {
    const { rerender } = renderHook(
      ({ s }: { s: ConversationStatus }) => useBuildNotifications(s),
      { initialProps: { s: "RUNNING" as ConversationStatus } },
    );
    rerender({ s: "AWAITING_USER_QUESTION" });
    expect(document.title).toMatch(/Needs your input/);
  });

  it("does NOT badge when the tab is visible (no noise)", () => {
    setHidden(false);
    const { rerender } = renderHook(
      ({ s }: { s: ConversationStatus }) => useBuildNotifications(s),
      { initialProps: { s: "RUNNING" as ConversationStatus } },
    );
    rerender({ s: "FINISHED" });
    expect(document.title).toBe(BASE);
  });

  it("restores the pristine title when the tab refocuses", () => {
    const { rerender } = renderHook(
      ({ s }: { s: ConversationStatus }) => useBuildNotifications(s),
      { initialProps: { s: "RUNNING" as ConversationStatus } },
    );
    rerender({ s: "FINISHED" });
    expect(document.title).not.toBe(BASE);
    act(() => {
      setHidden(false);
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(document.title).toBe(BASE);
  });

  it("fires an OS Notification when permission is granted and the tab is hidden", () => {
    const ctor = vi.fn();
    class FakeNotification {
      static permission = "granted";
      static requestPermission = vi.fn();
      constructor(title: string, opts?: unknown) {
        ctor(title, opts);
      }
    }
    vi.stubGlobal("Notification", FakeNotification);
    const { rerender } = renderHook(
      ({ s }: { s: ConversationStatus }) => useBuildNotifications(s, "make fizzbuzz"),
      { initialProps: { s: "RUNNING" as ConversationStatus } },
    );
    rerender({ s: "FINISHED" });
    expect(ctor).toHaveBeenCalledTimes(1);
    expect(ctor.mock.calls[0][0]).toMatch(/Build finished/);
    expect(ctor.mock.calls[0][1]).toEqual({ body: "make fizzbuzz" });
  });
});
