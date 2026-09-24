import { act, renderHook } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { useElementSelect } from "./useElementSelect";

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
