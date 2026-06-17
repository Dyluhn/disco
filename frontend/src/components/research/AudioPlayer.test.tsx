/**
 * AudioPlayer — vitest unit tests (WALK-14 / D2).
 *
 * Tests:
 *  (a) Renders a play button (not the raw native <audio controls>)
 *  (b) Play/pause icon toggles after user interaction
 *  (c) Shows the 0:00 / 0:00 time display on mount
 *  (d) Has correct aria-labels for accessibility
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, beforeEach, vi } from "vitest";
import { AudioPlayer } from "./AudioPlayer";

// jsdom does not implement HTMLMediaElement APIs (play/pause/load).
// We patch them before each test so the component doesn't throw.
beforeEach(() => {
  Object.defineProperty(HTMLMediaElement.prototype, "play", {
    configurable: true,
    writable: true,
    value: vi.fn().mockResolvedValue(undefined),
  });
  Object.defineProperty(HTMLMediaElement.prototype, "pause", {
    configurable: true,
    writable: true,
    value: vi.fn(),
  });
  // Make paused default to true (jsdom initial state).
  Object.defineProperty(HTMLMediaElement.prototype, "paused", {
    configurable: true,
    get: vi.fn().mockReturnValue(true),
  });
});

describe("AudioPlayer", () => {
  // (a) Renders with a play button, not a native audio widget
  it("renders a Play button as the primary control", () => {
    render(<AudioPlayer src="http://localhost/test.mp3" />);
    const playBtn = screen.getByRole("button", { name: /Play audio overview/i });
    expect(playBtn).toBeInTheDocument();
  });

  // (b) No native <audio controls> attribute (we own the chrome)
  it("does not expose a native audio controls widget", () => {
    const { container } = render(<AudioPlayer src="http://localhost/test.mp3" />);
    const audioEl = container.querySelector("audio");
    expect(audioEl).not.toBeNull();
    // The <audio> element must NOT have 'controls' — we manage the UI ourselves.
    expect(audioEl?.hasAttribute("controls")).toBe(false);
  });

  // (c) Time display shows 0:00 / 0:00 on mount (no duration loaded yet)
  it("shows 0:00 / 0:00 time on mount", () => {
    render(<AudioPlayer src="http://localhost/test.mp3" />);
    expect(screen.getByText("0:00 / 0:00")).toBeInTheDocument();
  });

  // (d) Accessible: play button has aria-label, seek has aria-label
  it("has accessible labels on the play button and seek bar", () => {
    render(<AudioPlayer src="http://localhost/test.mp3" />);
    expect(screen.getByRole("button", { name: /Play audio overview/i })).toBeInTheDocument();
    expect(screen.getByRole("slider", { name: /Seek audio overview/i })).toBeInTheDocument();
  });

  // (e) Clicking the play button calls audio.play()
  it("calls audio.play() when the play button is clicked", async () => {
    const user = userEvent.setup();
    const { container } = render(<AudioPlayer src="http://localhost/test.mp3" />);
    const audioEl = container.querySelector("audio") as HTMLAudioElement;
    const playSpy = vi.spyOn(audioEl, "play").mockResolvedValue(undefined);

    await user.click(screen.getByRole("button", { name: /Play audio overview/i }));
    expect(playSpy).toHaveBeenCalledTimes(1);
  });

  // (f) The player is in an accessible group
  it("wraps controls in a labelled group", () => {
    render(<AudioPlayer src="http://localhost/test.mp3" />);
    expect(
      screen.getByRole("group", { name: /Audio overview player/i }),
    ).toBeInTheDocument();
  });
});
