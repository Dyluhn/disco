import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useMemo, useRef, useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { ElementMentionChip } from "@/components/build/ElementMentionChip";
import { SteerInput } from "@/components/build/SteerInput";
import {
  parseElementMentionMessage,
  prependElementMention,
} from "@/lib/elementMention";
import type { ElementMentionPayload } from "@/lib/elementMention";
import { useElementMention } from "@/hooks/useElementMention";

const payload: ElementMentionPayload = {
  domPath: ["button#save.primary", "section.panel", "main"],
  reactPath: ["SaveButton", "SettingsPanel"],
  screenLabel: "Settings",
  text: "Save changes",
  rect: { x: 12, y: 34, w: 120, h: 32 },
  href: "https://preview.test/save",
};

function Harness({
  onSend,
  postMessage,
}: {
  onSend: (content: string) => void;
  postMessage: ReturnType<typeof vi.fn>;
}) {
  const fakeFrame = useMemo(
    () => ({ contentWindow: { postMessage } }) as unknown as HTMLIFrameElement,
    [postMessage],
  );
  const iframeRef = useRef<HTMLIFrameElement | null>(fakeFrame);
  const [mention, setMention] = useState<ElementMentionPayload | null>(null);
  const picker = useElementMention(iframeRef, "https://preview.test", setMention);

  return (
    <div>
      <button type="button" onClick={picker.arm}>
        Point
      </button>
      <ElementMentionChip mention={mention} onRemove={() => setMention(null)} />
      <SteerInput
        onSteer={(text) => {
          onSend(prependElementMention(text, mention));
          setMention(null);
        }}
      />
    </div>
  );
}

describe("useElementMention", () => {
  it("arms the iframe, turns a valid picker message into a chip, and serializes on send", async () => {
    const user = userEvent.setup();
    const sent = vi.fn();
    const postMessage = vi.fn();
    render(<Harness onSend={sent} postMessage={postMessage} />);

    await user.click(screen.getByRole("button", { name: "Point" }));
    expect(postMessage).toHaveBeenCalledWith(
      expect.objectContaining({ type: "disco-element-mention:arm" }),
      "*",
    );
    const armFrame = postMessage.mock.calls[0][0] as { nonce: string };

    act(() => {
      window.dispatchEvent(
        new MessageEvent("message", {
          origin: "https://preview.test",
          data: { type: "disco-element-mention", nonce: armFrame.nonce, payload },
        }),
      );
    });

    expect(await screen.findByTestId("element-mention-chip")).toHaveTextContent(
      "Element: <button> - Save changes",
    );

    await user.type(screen.getByLabelText("Steer the agent"), "Make it green");
    await user.click(screen.getByRole("button", { name: "Send steer" }));

    await waitFor(() => expect(sent).toHaveBeenCalledTimes(1));
    const content = sent.mock.calls[0][0] as string;
    expect(content).toContain("<mentioned-element>\ndom: button#save.primary");
    expect(content).toContain("react: SaveButton > SettingsPanel");
    expect(content).toContain("screen: Settings");
    expect(content).toContain("text: Save changes");
    expect(content).toContain("rect: x=12 y=34 w=120 h=32");
    expect(content.endsWith("Make it green")).toBe(true);
    expect(screen.queryByTestId("element-mention-chip")).not.toBeInTheDocument();
  });

  it("rejects wrong-origin and malformed picker messages", () => {
    const valid = new MessageEvent("message", {
      origin: "https://preview.test",
      data: { type: "disco-element-mention", nonce: "n", payload },
    });
    expect(parseElementMentionMessage(valid, "https://preview.test", "n")).toEqual(payload);

    const wrongOrigin = new MessageEvent("message", {
      origin: "https://evil.test",
      data: { type: "disco-element-mention", nonce: "n", payload },
    });
    expect(parseElementMentionMessage(wrongOrigin, "https://preview.test", "n")).toBeNull();

    const badShape = new MessageEvent("message", {
      origin: "https://preview.test",
      data: {
        type: "disco-element-mention",
        nonce: "n",
        payload: { ...payload, rect: { x: 1, y: 2, w: "wide", h: 4 } },
      },
    });
    expect(parseElementMentionMessage(badShape, "https://preview.test", "n")).toBeNull();

    const wrongNonce = new MessageEvent("message", {
      origin: "https://preview.test",
      data: { type: "disco-element-mention", nonce: "old", payload },
    });
    expect(parseElementMentionMessage(wrongNonce, "https://preview.test", "n")).toBeNull();
  });
});
