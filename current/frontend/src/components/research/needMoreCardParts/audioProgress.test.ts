/**
 * Regression: the audio-overview failure card showed a bare pipeline stage
 * name ("Audio overview failed: turn_script") and nothing else — no reason, no
 * next step — while the server's own explanation sat unused in the SSE frame.
 */

import { describe, expect, it } from "vitest";
import { audioReasonDetail, audioReasonMessage } from "./audioProgress";

describe("audio failure copy", () => {
  it("shows the server's message rather than the raw stage name", () => {
    const message =
      "Couldn't write the narration script for this report. This is the model " +
      "talking, not the voice: check the model assigned to answering in " +
      "Settings → Models, then try again.";
    expect(audioReasonMessage("turn_script", message)).toBe(message);
  });

  it("never degrades to a bare stage name when the server sends no message", () => {
    expect(audioReasonMessage("turn_script")).toBe(
      "Audio overview failed at the turn_script stage.",
    );
  });

  it("keeps the stage and the server detail as the secondary line", () => {
    expect(audioReasonDetail("turn_script", "total_turns must be between 12 and 20")).toBe(
      "turn_script: total_turns must be between 12 and 20",
    );
    expect(audioReasonDetail("tts_disabled", undefined)).toBe("tts_disabled");
  });

  it("still special-cases the disabled-in-settings reason", () => {
    expect(audioReasonMessage("tts_disabled")).toContain("Settings → Audio");
  });
});
