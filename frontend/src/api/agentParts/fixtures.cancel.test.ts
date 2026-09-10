import { afterEach, expect, it, vi } from "vitest";
import { subscribeDeepFixture } from "./fixtures";

afterEach(() => vi.useRealTimers());

it("does not deliver a delayed research frame after unsubscribe", async () => {
  vi.useFakeTimers();
  const onFrame = vi.fn();
  const handle = subscribeDeepFixture(onFrame);
  const count = onFrame.mock.calls.length;
  handle.cancel();
  await vi.runAllTimersAsync();
  expect(onFrame).toHaveBeenCalledTimes(count);
});
