import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ConversationStatus } from "@/types/agent";
import {
  resetDeepResearchDoneNotificationStateForTests,
  useDeepResearchDoneNotification,
} from "./useDeepResearchDoneNotification";

describe("useDeepResearchDoneNotification", () => {
  beforeEach(() => {
    resetDeepResearchDoneNotificationStateForTests();
  });

  afterEach(() => {
    resetDeepResearchDoneNotificationStateForTests();
    vi.unstubAllGlobals();
  });

  it("arms notification state and requests browser permission on first toggle", async () => {
    class FakeNotification {
      static permission: NotificationPermission = "default";
      static requestPermission = vi.fn().mockResolvedValue("granted");
    }
    vi.stubGlobal("Notification", FakeNotification);
    const toast = { show: vi.fn() };

    const { result } = renderHook(() =>
      useDeepResearchDoneNotification({
        cid: "deep_1",
        status: "RUNNING",
        title: "Battery report",
        toast,
      }),
    );

    expect(result.current.armed).toBe(false);
    await act(async () => {
      result.current.toggle();
    });

    expect(result.current.armed).toBe(true);
    expect(FakeNotification.requestPermission).toHaveBeenCalledTimes(1);
  });

  it("fires a toast and browser Notification when an armed run reaches FINISHED", async () => {
    const notificationCtor = vi.fn();
    class FakeNotification {
      static permission: NotificationPermission = "granted";
      static requestPermission = vi.fn();
      constructor(title: string) {
        notificationCtor(title);
      }
    }
    vi.stubGlobal("Notification", FakeNotification);
    const toast = { show: vi.fn(() => 1) };

    const { result, rerender } = renderHook(
      ({ status }: { status: ConversationStatus }) =>
        useDeepResearchDoneNotification({
          cid: "deep_1",
          status,
          title: "Battery report",
          toast,
        }),
      { initialProps: { status: "RUNNING" as ConversationStatus } },
    );

    await act(async () => {
      result.current.toggle();
    });
    rerender({ status: "FINISHED" });

    expect(toast.show).toHaveBeenCalledWith({
      title: "Deep research complete — Battery report",
    });
    expect(notificationCtor).toHaveBeenCalledWith(
      "Deep research complete — Battery report",
    );
    expect(result.current.armed).toBe(false);
  });
});
