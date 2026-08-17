import {
  forwardRef,
  useCallback,
  useEffect,
  useRef,
  type SyntheticEvent,
  type ComponentPropsWithoutRef,
} from "react";
import type { PreviewLaunch } from "@/api/preview";
import { postPreviewLaunch } from "@/lib/previewLaunch";

let frameLaunchCounter = 0;

type PreviewLaunchFrameProps = Omit<
  ComponentPropsWithoutRef<"iframe">,
  "name" | "src"
> & {
  launch: PreviewLaunch;
};

type PreviewLaunchFrameInstanceProps = PreviewLaunchFrameProps & {
  launchIdentity: string;
};

const PreviewLaunchFrameInstance = forwardRef<
  HTMLIFrameElement,
  PreviewLaunchFrameInstanceProps
>(
  function PreviewLaunchFrameInstance(
    { launch, launchIdentity, onLoad, ...props },
    forwardedRef,
  ) {
    const { url: launchUrl, intent: launchIntent } = launch;
    // The target is bearer-free but unique to this exact launch. React's useId
    // can remain stable at the same tree position across keyed remounts, which
    // would let launch A's delayed named form navigate launch B's fresh frame.
    const name = `disco-preview-frame-${launchIdentity}`;
    const internalRef = useRef<HTMLIFrameElement | null>(null);
    const submittedLaunchRef = useRef<string | null>(null);
    const targetLoadArmedRef = useRef(false);
    const bootstrapReadySeenRef = useRef(false);
    const setRef = useCallback(
      (node: HTMLIFrameElement | null) => {
        internalRef.current = node;
        if (typeof forwardedRef === "function") forwardedRef(node);
        else if (forwardedRef) forwardedRef.current = node;
      },
      [forwardedRef],
    );

    useEffect(() => {
      if (!launchIntent) return;
      const onBootstrapReady = (event: MessageEvent) => {
        if (
          !bootstrapReadySeenRef.current &&
          event.source === internalRef.current?.contentWindow &&
          event.data === "disco-preview-bootstrap-ready"
        ) {
          // Ignore about:blank and the POST trampoline's own load. The next
          // iframe load is the exact signed target selected by the trampoline.
          targetLoadArmedRef.current = true;
          bootstrapReadySeenRef.current = true;
          window.removeEventListener("message", onBootstrapReady);
        }
      };
      window.addEventListener("message", onBootstrapReady);
      return () => window.removeEventListener("message", onBootstrapReady);
    }, [launchIntent]);

    useEffect(() => {
      if (!internalRef.current) return;
      if (!launchIntent) return;
      const launchKey = `${launchUrl}\0${launchIntent}`;
      // React StrictMode deliberately replays effects. One-time bearer intents
      // must be submitted once per mounted frame, not once per effect setup.
      if (submittedLaunchRef.current === launchKey) return;
      submittedLaunchRef.current = launchKey;
      postPreviewLaunch({ url: launchUrl, intent: launchIntent }, name);
    }, [launchIntent, launchUrl, name]);

    const handleLoad = (event: SyntheticEvent<HTMLIFrameElement>) => {
      if (!launchIntent || targetLoadArmedRef.current) {
        targetLoadArmedRef.current = false;
        onLoad?.(event);
      }
    };

    return (
      <iframe
        {...props}
        ref={setRef}
        name={name}
        src={launchIntent ? undefined : launchUrl}
        onLoad={handleLoad}
      />
    );
  },
);

export const PreviewLaunchFrame = forwardRef<HTMLIFrameElement, PreviewLaunchFrameProps>(
  function PreviewLaunchFrame(props, forwardedRef) {
    // The one-time handoff message is intentionally bearer-free. A keyed child
    // gives every exact launch a fresh browsing context and fresh load gate, so
    // a late message/load from launch A cannot arm launch B.
    const launchKey = `${props.launch.url}\0${props.launch.intent}`;
    const identityRef = useRef({ launchKey: "", identity: "" });
    if (identityRef.current.launchKey !== launchKey) {
      frameLaunchCounter = (frameLaunchCounter + 1) % Number.MAX_SAFE_INTEGER;
      identityRef.current = {
        launchKey,
        identity: `preview-launch-${frameLaunchCounter}`,
      };
    }
    return (
      <PreviewLaunchFrameInstance
        key={identityRef.current.identity}
        {...props}
        launchIdentity={identityRef.current.identity}
        ref={forwardedRef}
      />
    );
  },
);
