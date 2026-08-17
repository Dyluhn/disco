import type { PreviewLaunch } from "@/api/client";

let fallbackTargetCounter = 0;

function targetName(prefix: string): string {
  const randomValues = globalThis.crypto?.getRandomValues?.bind(globalThis.crypto);
  if (randomValues) {
    const words = randomValues(new Uint32Array(4));
    return `${prefix}-${Array.from(words, (word) => word.toString(16).padStart(8, "0")).join("")}`;
  }
  // Target names aren't credentials. This bounded fallback only needs to avoid
  // collisions on old/plain-HTTP browsers without Web Crypto.
  fallbackTargetCounter = (fallbackTargetCounter + 1) % Number.MAX_SAFE_INTEGER;
  return `${prefix}-${Date.now().toString(36)}-${fallbackTargetCounter.toString(36)}`;
}

export function postPreviewLaunch(launch: PreviewLaunch, target: string): void {
  const form = document.createElement("form");
  form.method = "POST";
  form.action = launch.url;
  form.target = target;
  form.rel = "noopener noreferrer";
  form.style.display = "none";
  const intent = document.createElement("input");
  intent.type = "hidden";
  intent.name = "intent";
  intent.value = launch.intent;
  form.append(intent);
  document.body.append(form);
  form.submit();
  // Submission serializes synchronously. Remove bearer material from the DOM
  // immediately so snapshots/telemetry never retain a reusable hidden input.
  intent.value = "";
  form.remove();
}

export function openFreshPreview(
  mint: () => Promise<PreviewLaunch | null>,
  onError?: (reason: "popup_blocked" | "capability_unavailable") => void,
): Window | null {
  const name = targetName("disco-preview-popup");
  const popup = window.open("about:blank", name);
  if (!popup) {
    onError?.("popup_blocked");
    return null;
  }
  try {
    popup.opener = null;
  } catch {
    // The form's rel=noopener is the browser-enforced fallback.
  }
  void mint()
    .then((launch) => {
      if (!launch) {
        popup.close();
        onError?.("capability_unavailable");
        return;
      }
      postPreviewLaunch(launch, name);
    })
    .catch(() => {
      popup.close();
      onError?.("capability_unavailable");
    });
  return popup;
}
