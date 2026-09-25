import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useElementSelect } from "./useElementSelect";

afterEach(() => vi.unstubAllGlobals());

it("disarm clears the visible selection and rejects messages from the dismissed nonce", () => {
  const postMessage = vi.fn();
  const frame = { current: { contentWindow: { postMessage } } as unknown as HTMLIFrameElement };
  const { result } = renderHook(() => useElementSelect(frame, "https://preview.test"));
  act(() => result.current.arm());
  const nonce = postMessage.mock.calls[0][0].nonce;
  const selection = new MessageEvent("message", { origin: "https://preview.test", data: {
    v: 1, channel: "disco-select", nonce, type: "disco:selection",
    selection_ref: { kind: "source", oid: "index.html:1:1", file: "index.html", line: 1 },
    human_label: "Heading", rect: { x: 1, y: 1, width: 50, height: 20 },
  } });
  act(() => window.dispatchEvent(selection));
  expect(result.current.selection?.human_label).toBe("Heading");
  act(() => result.current.disarm());
  expect(result.current.armed).toBe(false);
  expect(result.current.selection).toBeNull();
  expect(postMessage.mock.calls.at(-1)?.[0].type).toBe("disco:overlay:disarm");
  act(() => window.dispatchEvent(selection));
  expect(result.current.selection).toBeNull();
});

function resizeHarness() {
  const observers: {
    notify: (target: HTMLIFrameElement) => void;
    observe: ReturnType<typeof vi.fn>;
    disconnect: ReturnType<typeof vi.fn>;
  }[] = [];
  vi.stubGlobal("ResizeObserver", class {
    observe = vi.fn();
    disconnect = vi.fn();
    constructor(callback: ResizeObserverCallback) {
      observers.push({
        observe: this.observe,
        disconnect: this.disconnect,
        notify: (target) => callback(
          [{ target } as ResizeObserverEntry], this as unknown as ResizeObserver,
        ),
      });
    }
  });
  const postMessage = vi.fn();
  const dimensions = { width: 800, height: 600 };
  const node = {
    contentWindow: { postMessage },
    get clientWidth() { return dimensions.width; },
    get clientHeight() { return dimensions.height; },
  } as unknown as HTMLIFrameElement;
  const frame = { current: node };
  const hook = renderHook(() => useElementSelect(frame, "https://preview.test"));
  act(() => hook.result.current.arm());
  const nonce = postMessage.mock.calls[0][0].nonce;
  const select = () => act(() => window.dispatchEvent(new MessageEvent("message", {
    origin: "https://preview.test",
    data: {
      v: 1, channel: "disco-select", nonce, type: "disco:selection",
      selection_ref: { kind: "source", oid: "index.html:1:1", file: "index.html", line: 1 },
      human_label: "Heading", rect: { x: 1, y: 1, width: 50, height: 20 },
    },
  })));
  select();
  return { ...hook, frame, node, dimensions, observers, postMessage, select };
}

it("keeps the selected element on an unchanged initial resize observation", () => {
  const { result, node, observers } = resizeHarness();
  const observer = observers.at(-1)!;
  expect(observer.observe).toHaveBeenCalledWith(node);
  act(() => observer.notify(node));
  expect(result.current.selection?.human_label).toBe("Heading");
  expect(result.current.armed).toBe(true);
});

it.each(["width", "height"] as const)("clears stale selection on changed frame %s without disarming", (axis) => {
  const { result, node, dimensions, observers, postMessage, select } = resizeHarness();
  dimensions[axis] += 80;
  act(() => observers.at(-1)!.notify(node));
  expect(result.current.selection).toBeNull();
  expect(result.current.armed).toBe(true);
  expect(postMessage).toHaveBeenCalledTimes(1);
  select();
  expect(result.current.selection?.human_label).toBe("Heading");
  act(() => observers.at(-1)!.notify(node));
  expect(result.current.selection?.human_label).toBe("Heading");
});

it("retains the measured size across rerenders before resize delivery", () => {
  const { result, node, dimensions, observers, rerender } = resizeHarness();
  const prior = observers.at(-1)!;
  dimensions.height += 80;
  rerender();
  expect(prior.disconnect).toHaveBeenCalledOnce();
  act(() => observers.at(-1)!.notify(node));
  expect(result.current.selection).toBeNull();
  expect(result.current.armed).toBe(true);
});

it("disconnects on disarm and unmount and ignores callbacks for a replaced frame", () => {
  const { result, frame, node, dimensions, observers, rerender, unmount } = resizeHarness();
  const original = observers.at(-1)!;
  frame.current = { ...node, clientWidth: 500, clientHeight: 400 };
  dimensions.width += 80;
  act(() => original.notify(node));
  expect(result.current.selection?.human_label).toBe("Heading");
  rerender();
  expect(original.disconnect).toHaveBeenCalledOnce();
  expect(observers.at(-1)!.observe).toHaveBeenCalledWith(frame.current);
  const current = observers.at(-1)!;
  act(() => result.current.disarm());
  expect(current.disconnect).toHaveBeenCalledOnce();
  expect(result.current.armed).toBe(false);
  act(() => result.current.arm());
  const last = observers.at(-1)!;
  unmount();
  expect(last.disconnect).toHaveBeenCalledOnce();
  act(() => last.notify(frame.current));
});
