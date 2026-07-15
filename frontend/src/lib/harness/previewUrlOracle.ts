export const ISOLATED_PREVIEW_PREFIX = "/__disco/isolated-preview";

const LIVE_PREVIEW_HOST = /^p2-[0-9a-f]{8}-\d{2,5}\./;
const STATIC_PREVIEW_HOST = /^p3s-[0-9a-f]{8}-[0-9a-f]{40}-\d{2,5}\./;

/**
 * True for every product-owned isolated preview response surface observed by
 * the live harness: the p2 live-port origin, the p3s committed-static origin,
 * and the path-preview route used when wildcard routing is unavailable.
 */
export function isIsolatedPreviewUrl(rawUrl: string): boolean {
  try {
    const url = new URL(rawUrl);
    return (
      url.pathname.startsWith(`${ISOLATED_PREVIEW_PREFIX}/`) ||
      LIVE_PREVIEW_HOST.test(url.hostname) ||
      STATIC_PREVIEW_HOST.test(url.hostname)
    );
  } catch {
    return false;
  }
}
