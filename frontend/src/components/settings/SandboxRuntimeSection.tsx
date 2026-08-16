import { useSandboxConfig } from "@/hooks/useModels";
import { backendSupportsLiveView } from "@/types/sandbox";
import { LiveBrowserSection } from "./LiveBrowserSection";
import { SandboxSection } from "./SandboxSection";

/** Keep the dependent live-browser control at the sandbox decision point. */
export function SandboxRuntimeSection() {
  const { data } = useSandboxConfig();
  const liveSupported = backendSupportsLiveView(data?.backend);

  return (
    <div className="flex flex-col gap-section">
      <SandboxSection />
      <div
        id="live-browser"
        className={data ? "border-t border-hairline pt-section" : undefined}
      >
        {data &&
          (liveSupported ? (
            <LiveBrowserSection />
          ) : (
            <p
              data-live-browser-relevance="gvisor-only"
              className="rounded-control border border-hairline bg-surface-1/30 px-body py-inline font-ui text-[0.78rem] text-text-faint"
            >
              Live browser settings appear here when gVisor is active. Other
              sandboxes keep the lightweight screenshot reel.
            </p>
          ))}
      </div>
    </div>
  );
}
