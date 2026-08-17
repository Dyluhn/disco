import { useMemo, useState } from "react";
import { deriveLiveSignal } from "@/lib/buildTrace";
import type { AgentEvent, ConversationStatus } from "@/types/agent";
import { PreviewLaunchFrame } from "@/components/PreviewLaunchFrame";
import { workspaceFileUrl } from "@/api/canvas";
import { Empty } from "./Empty";
import { WorkspaceImage } from "./WorkspaceImage";
import { BrowserPaneHeader } from "./BrowserPaneHeader";
import { LiveFailureBanner } from "./LiveFailureBanner";
import { ScreenshotStrip } from "./ScreenshotStrip";
import { useLiveBrowserSession } from "./useLiveBrowserSession";
import { collectScreenshots, latestBrowserUrl, pickHeroScreenshot, isDrivingBrowser } from "./browserEvents";

export function BrowserPane({
  events,
  status,
  cid,
}: {
  events: AgentEvent[];
  status: ConversationStatus;
  cid: string | null;
}) {
  const shots = useMemo(() => collectScreenshots(events), [events]);
  const url = useMemo(() => latestBrowserUrl(events), [events]);
  // "driving…" while a browser / external browser-MCP tool is executing.
  const driving = isDrivingBrowser(deriveLiveSignal(events, status));
  const [picked, setPicked] = useState<string | null>(null);
  const hero = pickHeroScreenshot(picked, shots);

  const { liveView, liveStartFailure, streaming, setIframeConnected } = useLiveBrowserSession(cid);

  // When live view is active, show the noVNC iframe instead of screenshots.
  if (liveView && cid) {
    return (
      <div className="flex h-full min-h-0 flex-col">
        <BrowserPaneHeader url={url} driving={driving} streaming={streaming} liveViewActive />
        <div className="flex min-h-0 flex-1 flex-col">
          <PreviewLaunchFrame
            title="Live browser (noVNC)"
            launch={liveView.launch}
            // The iframe genuinely LOADING is the truth behind the green-blink badge.
            onLoad={() => setIframeConnected(true)}
            // The capability host is cross-origin from the app. same-origin is
            // required inside the frame so noVNC's websocket has its real Origin;
            // cross-origin browser isolation still prevents access to this page.
            sandbox="allow-scripts allow-forms allow-same-origin"
            className="h-full w-full border-0"
            data-testid="novnc-iframe"
          />
        </div>
      </div>
    );
  }

  // No screenshot we can actually load (none captured, or no cid to fetch against)
  // → an honest empty state, never a broken <img>. Auto-start failure also lands
  // here with the failure banner, so the screenshot fallback is explicit.
  if (!hero || !cid) {
    return (
      <div className="flex h-full min-h-0 flex-col" data-screenshot-state="empty">
        <BrowserPaneHeader url={url} driving={driving} streaming={streaming} liveViewActive={false} />
        <LiveFailureBanner failure={liveStartFailure} />
        <Empty>
          <p className="text-text-muted">
            When the agent uses a browser, the pages it visits show here — a screenshot for each
            step, updated as it navigates (a frame-by-frame reel, not a live video).
          </p>
          <p className="max-w-measure">
            External browser-MCP servers return fenced text, not screenshots — for those this shows
            status, not a faked viewport.
          </p>
        </Empty>
      </div>
    );
  }

  const heroSrc = workspaceFileUrl(cid, hero);
  return (
    <div className="flex h-full min-h-0 flex-col">
      <BrowserPaneHeader url={url} driving={driving} streaming={streaming} liveViewActive={false} />
      <LiveFailureBanner failure={liveStartFailure} />
      <div className="flex min-h-0 flex-1 items-center justify-center overflow-auto bg-[oklch(0.15_0.005_260)] p-inline">
        <WorkspaceImage
          key={heroSrc}
          src={heroSrc}
          className="max-h-full max-w-full rounded border border-hairline object-contain"
        />
      </div>
      <ScreenshotStrip shots={shots} hero={hero} cid={cid} onPick={setPicked} />
    </div>
  );
}
