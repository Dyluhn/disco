/** The Browser pane's top bar: the URL of the last page the agent drove to
 * (or "browser" while no URL has been seen yet), plus the "Live" / "driving…"
 * liveness pulse. Rendered only when there's something honest to show. */
export function BrowserPaneHeader({
  url,
  driving,
  streaming,
  liveViewActive,
}: {
  url: string | null;
  driving: boolean;
  streaming: boolean;
  liveViewActive: boolean;
}) {
  if (!url && !driving && !liveViewActive) return null;
  return (
    <div className="flex shrink-0 items-center justify-between gap-inline border-b border-hairline px-body py-hair">
      <span className="truncate font-mono text-[0.74rem] text-text-faint" title={url ?? undefined}>
        {url ?? "browser"}
      </span>
      <div className="flex shrink-0 items-center gap-inline">
        {/* genuinely streaming → the honest green-blink "Live" badge (no ambiguity) */}
        {streaming && (
          <span
            className="flex shrink-0 items-center gap-hair font-ui text-[0.72rem] font-medium text-[oklch(0.74_0.17_150)]"
            data-disco-control="agent.live-browser"
            data-streaming="true"
            data-testid="live-badge"
            aria-label="Live browser streaming"
          >
            <span
              className="size-1.5 animate-pulse rounded-full bg-[oklch(0.74_0.17_150)]"
              aria-hidden
            />
            Live
          </span>
        )}
        {/* not yet streaming but the agent is actively driving a browser → the existing
            "driving…" pulse (the live view auto-starts behind this when streamable) */}
        {driving && !streaming && (
          <span className="flex shrink-0 items-center gap-hair font-ui text-[0.72rem] text-accent">
            <span className="size-1.5 animate-pulse rounded-full bg-accent" aria-hidden />
            driving…
          </span>
        )}
      </div>
    </div>
  );
}
