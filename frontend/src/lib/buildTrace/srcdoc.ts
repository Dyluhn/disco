/**
 * Cluster 5 (UI 2.1): assemble HTML from the written files for a CLIENT-SIDE
 * `srcdoc` preview. Split out of `buildTrace.ts` verbatim (no violation on this
 * callable; moved purely to bring the module below the size cap).
 */
import type { WorkspaceFile } from "../buildTrace";

/** Picks the entry HTML (index.html, else any *.html). With no conversation URL
 * it inlines known local CSS/JS as an offline fallback; with a preview base it
 * leaves assets external so all relative file classes preserve browser URL
 * semantics. Returns null when there's no renderable HTML artifact (so the pane
 * falls back to the live-server preview / placeholder).
 *
 * @param injectionScript - Optional JavaScript source to inject as the FIRST
 *   `<script>` inside `<body>` (or appended when no body tag is present).
 *   Used by the selection overlay (§4.1 / C-EDIT-1) to install the in-frame
 *   selection agent.  Pass `undefined` for untrusted / no-scripts iframes.
 * @param baseUrl - Optional authenticated preview route used as the document
 *   base.  A srcdoc document otherwise has no useful URL, so images, fonts,
 *   nested links, and runtime fetches with relative URLs all fail even when the
 *   corresponding workspace files exist.
 * @param selectedEntryPath - Optional canonical handoff path from the latest
 *   DeliverableEvent. When present, that exact HTML is the only eligible entry;
 *   a missing/empty selection fails closed instead of falling back to a stale
 *   workspace-root index.html.
 */
export function deriveSrcDoc(
  files: WorkspaceFile[],
  injectionScript?: string,
  baseUrl?: string,
  selectedEntryPath?: string,
): string | null {
  if (files.length === 0) return null;
  const byName = new Map<string, string>();
  for (const f of files) {
    // index by basename and by path so both `href="style.css"` and
    // `href="./css/style.css"` resolve.
    byName.set(f.path, f.content);
    byName.set(f.path.split("/").pop() ?? f.path, f.content);
  }
  const selectedPath = selectedEntryPath?.trim();
  const entry = selectedPath
    ? files.find((f) => f.path === selectedPath)
    : files.find((f) => /(^|\/)index\.html$/i.test(f.path)) ??
      files.find((f) => /\.html$/i.test(f.path));
  // C5: server-side artifacts land with content="" (bytes=0) — return null so the
  // existing `srcDoc != null` guards suppress the blank white frame and the pane
  // can fall back to the ?inline=true route or the placeholder instead.
  if (!entry || !entry.content) return null;
  let html = entry.content;
  if (baseUrl) {
    const escapedBase = baseUrl
      .replaceAll("&", "&amp;")
      .replaceAll('"', "&quot;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;");
    const baseTag = `<base href="${escapedBase}">`;
    if (/<head[\s>]/i.test(html)) {
      html = html.replace(/<head[\s>][^>]*>/i, (match) => `${match}\n${baseTag}`);
    } else if (/<html[\s>]/i.test(html)) {
      html = html.replace(/<html[\s>][^>]*>/i, (match) => `${match}\n<head>${baseTag}</head>`);
    } else {
      html = `<head>${baseTag}</head>\n${html}`;
    }
  }
  if (!baseUrl) {
    // Offline/no-conversation fallback: inline the two file classes the trace
    // carries completely.  With an authenticated base route, keep references
    // external so the browser preserves each asset's own URL base (especially
    // nested CSS url(../fonts/x.woff2)) and can load every file class generally.
    html = html.replace(
      /<link[^>]*rel=["']?stylesheet["']?[^>]*href=["']([^"']+)["'][^>]*>/gi,
      (m, href) => {
        const css = byName.get(href) ?? byName.get(href.replace(/^\.?\//, ""));
        return css != null ? `<style>\n${css}\n</style>` : m;
      },
    );
    html = html.replace(
      /<script[^>]*src=["']([^"']+)["'][^>]*><\/script>/gi,
      (m, src) => {
        const js = byName.get(src) ?? byName.get(src.replace(/^\.?\//, ""));
        return js != null ? `<script>\n${js}\n</script>` : m;
      },
    );
  }
  // Inject the selection agent script (§4.1 C-EDIT-1) when provided.
  // Injected as the first child of <body> so it runs before user scripts and
  // can intercept events; falls back to appending at the end when no <body>.
  if (injectionScript) {
    const tag = `<script>\n${injectionScript}\n</script>`;
    if (/<body[\s>]/i.test(html)) {
      html = html.replace(/<body[\s>][^>]*>/i, (m) => `${m}\n${tag}`);
    } else {
      html = `${tag}\n${html}`;
    }
  }
  return html;
}
