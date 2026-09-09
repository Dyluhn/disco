/**
 * AudioSection — the two UI failures a finished report showed on the VMs.
 *
 * UI-42: a report that ALREADY has audio came back, after a reload, showing
 *        only the "Audio Overview" button — no player, no Export MP3, no
 *        Regenerate — even though the server still held the artifact and
 *        handed it back a few seconds later. The section must ask on mount.
 * UI-23: once a player exists, "Audio Overview" and "Regenerate" both sat next
 *        to it and the operator had to guess which one re-runs the pipeline.
 * UI-43: "Regenerate" did not regenerate — it cleared the card back to the
 *        starting button and made the operator choose the mode over again.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const fetchExistingReportAudio = vi.fn();
const streamReportAudio = vi.fn();
const requestReportAudioBlocking = vi.fn();

vi.mock("@/api/deepResearch", () => ({
  absoluteAudioUrl: (url: string) => `http://agent${url}`,
  fetchExistingReportAudio: (cid: string) => fetchExistingReportAudio(cid),
  streamReportAudio: (...args: unknown[]) => streamReportAudio(...args),
  requestReportAudioBlocking: (...args: unknown[]) => requestReportAudioBlocking(...args),
}));

// The player is exercised by AudioPlayer.test.tsx; here it only needs to be
// findable, and jsdom has no HTMLMediaElement implementation.
vi.mock("../AudioPlayer", () => ({
  AudioPlayer: ({ src }: { src: string }) => <div data-testid="player" data-src={src} />,
}));

import { AudioSection } from "./AudioSection";

function renderSection(onRequestGenerate = vi.fn()) {
  render(
    <AudioSection
      cid="conv_bcc478d4"
      modeOpen={false}
      onModeOpenChange={vi.fn()}
      reportQuery="solid-state batteries"
      onRequestGenerate={onRequestGenerate}
    />,
  );
  return onRequestGenerate;
}

beforeEach(() => {
  vi.clearAllMocks();
  fetchExistingReportAudio.mockResolvedValue([]);
});

describe("AudioSection", () => {
  it("offers only the trigger when the server holds no audio", async () => {
    renderSection();
    expect(await screen.findByRole("button", { name: /Audio Overview/i })).toBeTruthy();
    await waitFor(() => expect(fetchExistingReportAudio).toHaveBeenCalledWith("conv_bcc478d4"));
    expect(screen.queryByTestId("player")).toBeNull();
  });

  // UI-42
  it("restores the player, Export MP3 and Regenerate for audio that already exists", async () => {
    fetchExistingReportAudio.mockResolvedValue([
      {
        mode: "single",
        note: "",
        mp3_url: "/conversations/conv_bcc478d4/report/audio/audio_overview_single_abc.mp3",
        transcript_url: "/conversations/conv_bcc478d4/report/audio/audio_overview_single_abc.md",
      },
    ]);
    renderSection();

    const player = await screen.findByTestId("player");
    expect(player.getAttribute("data-src")).toBe(
      "http://agent/conversations/conv_bcc478d4/report/audio/audio_overview_single_abc.mp3",
    );
    expect(screen.getByRole("button", { name: /Export MP3/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Regenerate/i })).toBeTruthy();
    // No generation run was started to find this out.
    expect(streamReportAudio).not.toHaveBeenCalled();
    expect(requestReportAudioBlocking).not.toHaveBeenCalled();
  });

  // UI-23
  it("drops the redundant Audio Overview trigger once a player exists", async () => {
    fetchExistingReportAudio.mockResolvedValue([
      {
        mode: "podcast",
        note: "",
        mp3_url: "/conversations/conv_bcc478d4/report/audio/audio_overview_podcast_abc.mp3",
        transcript_url: "/conversations/conv_bcc478d4/report/audio/audio_overview_podcast_abc.md",
      },
    ]);
    renderSection();

    await screen.findByTestId("player");
    expect(screen.queryByRole("button", { name: /^Audio Overview$/i })).toBeNull();
  });

  // UI-43
  it("re-runs the pipeline in the same mode, past the cache, on one press of Regenerate", async () => {
    fetchExistingReportAudio.mockResolvedValue([
      {
        mode: "single",
        note: "",
        mp3_url: "/conversations/conv_bcc478d4/report/audio/audio_overview_single_abc.mp3",
        transcript_url: "/conversations/conv_bcc478d4/report/audio/audio_overview_single_abc.md",
      },
    ]);
    streamReportAudio.mockImplementation(
      (_cid: string, _mode: string, _body: unknown, onEvent: (ev: unknown) => boolean) => {
        onEvent({ stage: "synthesizing", current: 1, total: 4 });
        onEvent({
          stage: "done",
          mode: "single",
          mp3_url: "/conversations/conv_bcc478d4/report/audio/audio_overview_single_def.mp3",
        });
        return Promise.resolve(true);
      },
    );
    const onRequestGenerate = renderSection();

    await screen.findByTestId("player");
    await userEvent.click(screen.getByRole("button", { name: /Regenerate/i }));

    // It generated rather than bouncing the operator back to the mode picker.
    expect(onRequestGenerate).not.toHaveBeenCalled();
    await waitFor(() => expect(streamReportAudio).toHaveBeenCalledTimes(1));
    const [cid, mode, , , force] = streamReportAudio.mock.calls[0];
    expect(cid).toBe("conv_bcc478d4");
    expect(mode).toBe("single"); // the mode this overview was made in
    expect(force).toBe(true); // past the content-hash cache: a real re-run

    // The artifact on the card is replaced by the new one.
    await waitFor(() =>
      expect(screen.getByTestId("player").getAttribute("data-src")).toContain(
        "audio_overview_single_def.mp3",
      ),
    );
  });

  it("shows the note when the narration was not simply the model's script", async () => {
    fetchExistingReportAudio.mockResolvedValue([
      {
        mode: "podcast",
        note: "The narration was built straight from the report's own sections.",
        mp3_url: "/conversations/conv_bcc478d4/report/audio/audio_overview_podcast_abc.mp3",
        transcript_url: "/conversations/conv_bcc478d4/report/audio/audio_overview_podcast_abc.md",
      },
    ]);
    renderSection();

    expect(
      await screen.findByText(/built straight from the report's own sections/i),
    ).toBeTruthy();
  });
});
