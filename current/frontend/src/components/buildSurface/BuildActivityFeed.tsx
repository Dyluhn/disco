/**
 * The Build surface's Activity heading + replay scrubber + scrollable feed
 * viewport + the docked "scroll to latest" chevron. The feed's actual content
 * (ERROR / plan-gate / running-activity) is passed as `children` so this
 * wrapper stays agnostic to that branching (see BuildActivityFeedContent).
 * Extracted verbatim from BuildSurface.tsx (PKG-12-FE-BUILD).
 */

import { ChevronDown } from "lucide-react";
import type { ReactNode } from "react";
import { ReplayScrubber } from "@/components/build/ReplayScrubber";
import type { ReplayState } from "@/lib/useReplay";

export function BuildActivityFeed({
  isReplaying,
  replay,
  eventsLength,
  feedScrollRef,
  onFeedScroll,
  showScrollDown,
  scrollFeedToBottom,
  children,
}: {
  isReplaying: boolean;
  replay: ReplayState;
  eventsLength: number;
  feedScrollRef: React.RefObject<HTMLDivElement | null>;
  onFeedScroll: () => void;
  showScrollDown: boolean;
  scrollFeedToBottom: () => void;
  children: ReactNode;
}) {
  return (
    // Mobile floor: below `lg` the chat pane is now a bounded, internally-
    // scrolling column (ResizableSplit fills the phone's height so the
    // composer stays put at the bottom — see that file's header comment). A
    // tall footer (the finished-handoff self-host card + the schedule
    // section) can otherwise squeeze this flex-1 wrapper to ~0px, and a
    // `position: sticky` child (the plan tracker) painted outside that
    // collapsed box rather than clipping cleanly — the feed looked like it
    // was overlapping the footer below it. A sane minimum height keeps the
    // feed always legible; if the footer still doesn't fit under it, IT
    // overflows the pane instead (reachable via the ordinary page scroll),
    // which reads as "scroll a bit further," not "broken." `lg:min-h-0`
    // restores the exact desktop behavior (untouched) at `lg`+.
    <div className="mt-section flex min-h-[16rem] flex-1 flex-col px-body lg:min-h-0">
      <div className="flex items-baseline justify-between">
        <h2 className="font-ui text-[0.72rem] font-medium uppercase tracking-wide text-text-faint">
          Activity
        </h2>
        <span
          className="font-ui text-[0.68rem] text-text-faint"
          title="The agent works one step at a time; an up-front plan preview lands with the planner step."
        >
          live task list
        </span>
      </div>
      {/* RP-06: replay scrubber — step through the event log when not live. */}
      {isReplaying && eventsLength > 0 && <ReplayScrubber replay={replay} />}
      <div
        ref={feedScrollRef}
        onScroll={onFeedScroll}
        data-testid="build-activity-feed"
        className="mt-inline min-h-0 flex-1 overflow-y-auto pb-inline lg:pr-hair"
      >
        {children}
      </div>
      {/* F15/W-41: the jump-to-latest control occupies its own flex row.
          Keeping it outside the scroll viewport reserves real layout space
          at every width/zoom and prevents it from covering transcript text. */}
      {showScrollDown && (
        <div
          data-testid="build-scroll-to-latest-dock"
          className="flex shrink-0 items-center justify-center py-hair"
        >
          <button
            type="button"
            onClick={scrollFeedToBottom}
            aria-label="Scroll to latest activity"
            title="Scroll to latest activity"
            data-disco-control="build.scroll-to-bottom"
            className="flex max-lg:size-11 items-center justify-center rounded-full border border-hairline bg-surface-1 p-inline text-text-muted shadow-sm transition-colors hover:bg-surface-2 hover:text-text"
          >
            <ChevronDown className="size-4" aria-hidden />
          </button>
        </div>
      )}
    </div>
  );
}
