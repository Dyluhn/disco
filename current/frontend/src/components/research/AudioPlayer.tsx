/**
 * AudioPlayer — custom audio player styled with the Disco design system (WALK-14 / D2).
 *
 * Replaces the bare native `<audio controls>` (browser default chrome) with a
 * purpose-built control surface that matches the rounded-control / border-hairline /
 * bg-surface-1 / font-ui / accent token vocabulary used everywhere else in the UI.
 *
 * Anatomy:
 *  - Hidden `<audio>` element (ref-controlled, not rendered as a widget)
 *  - Play / Pause button (lucide icons, aria-label)
 *  - Seek / progress bar (range input, accent fill via inline style)
 *  - Elapsed / total time (monospace tabular-nums)
 *
 * Accessible: all interactive elements have aria-labels; the hidden <audio> carries
 * aria-hidden so screen-readers don't see two competing controls.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Pause, Play } from "lucide-react";
import { cn } from "@/lib/cn";

interface AudioPlayerProps {
  /** The URL of the MP3 to play. */
  src: string;
  className?: string;
}

function fmtTime(seconds: number): string {
  if (!isFinite(seconds) || seconds < 0) return "0:00";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

export function AudioPlayer({ src, className }: AudioPlayerProps) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);

  // Wire audio element events → component state.
  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;

    const onPlay = () => setPlaying(true);
    const onPause = () => setPlaying(false);
    const onEnded = () => setPlaying(false);
    const onTimeUpdate = () => setCurrentTime(audio.currentTime);
    const onDurationChange = () => {
      if (isFinite(audio.duration)) setDuration(audio.duration);
    };
    const onLoadedMetadata = () => {
      if (isFinite(audio.duration)) setDuration(audio.duration);
    };

    audio.addEventListener("play", onPlay);
    audio.addEventListener("pause", onPause);
    audio.addEventListener("ended", onEnded);
    audio.addEventListener("timeupdate", onTimeUpdate);
    audio.addEventListener("durationchange", onDurationChange);
    audio.addEventListener("loadedmetadata", onLoadedMetadata);

    return () => {
      audio.removeEventListener("play", onPlay);
      audio.removeEventListener("pause", onPause);
      audio.removeEventListener("ended", onEnded);
      audio.removeEventListener("timeupdate", onTimeUpdate);
      audio.removeEventListener("durationchange", onDurationChange);
      audio.removeEventListener("loadedmetadata", onLoadedMetadata);
    };
  }, []);

  const togglePlay = useCallback(() => {
    const audio = audioRef.current;
    if (!audio) return;
    if (audio.paused) {
      void audio.play();
    } else {
      audio.pause();
    }
  }, []);

  const handleSeek = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const audio = audioRef.current;
    if (!audio) return;
    const value = Number(e.target.value);
    audio.currentTime = value;
    setCurrentTime(value);
  }, []);

  const progress = duration > 0 ? (currentTime / duration) * 100 : 0;

  return (
    <div
      className={cn(
        "flex items-center gap-inline rounded-control border border-hairline",
        "bg-surface-1 px-inline py-hair",
        className,
      )}
      role="group"
      aria-label="Audio overview player"
    >
      {/* Hidden real audio element — not a visible widget; state is managed above */}
      <audio ref={audioRef} src={src} preload="metadata" aria-hidden />

      {/* Play / Pause toggle */}
      <button
        type="button"
        onClick={togglePlay}
        aria-label={playing ? "Pause audio overview" : "Play audio overview"}
        className="shrink-0 rounded-control p-hair text-text transition-colors hover:text-accent focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent"
      >
        {playing ? (
          <Pause className="size-4" aria-hidden />
        ) : (
          <Play className="size-4" aria-hidden />
        )}
      </button>

      {/* Seek bar — accent-coloured fill via inline gradient */}
      <input
        type="range"
        min={0}
        max={duration || 100}
        step={0.1}
        value={currentTime}
        onChange={handleSeek}
        aria-label="Seek audio overview"
        aria-valuemin={0}
        aria-valuemax={duration || 100}
        aria-valuenow={currentTime}
        className="h-1 min-w-[5rem] flex-1 cursor-pointer appearance-none rounded-full"
        style={{
          background: `linear-gradient(to right, var(--accent, #6366f1) ${progress}%, var(--hairline, #e5e7eb) ${progress}%)`,
        }}
      />

      {/* Elapsed / total time */}
      <span
        className="shrink-0 font-ui text-[0.72rem] tabular-nums text-text-faint"
        aria-live="off"
      >
        {fmtTime(currentTime)}
        {" / "}
        {fmtTime(duration)}
      </span>
    </div>
  );
}
