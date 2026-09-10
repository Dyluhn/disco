/**
 * UI-10 regression — sandbox-local addresses never reach the user as links.
 *
 * The defect: a finished build's completion message said the site "is served via
 * preview at http://localhost:8000/". That is the SANDBOX's loopback; in the
 * user's browser it opens nothing. The scrubber rewrites every loopback address
 * (any port, any of the loopback aliases the backend's `_preview_key` collapses)
 * into a pointer at the Preview tab, and reports that it did so.
 */

import { describe, expect, it } from "vitest";
import { scrubSandboxUrls } from "@/lib/sandboxUrls";

describe("scrubSandboxUrls", () => {
  it("rewrites the exact UI-10 sentence and reports the rewrite", () => {
    const { text, replaced } = scrubSandboxUrls(
      "The site is complete and is served via preview at http://localhost:8000/.",
    );
    expect(replaced).toBe(true);
    expect(text).toBe(
      "The site is complete and is served via preview at the Preview tab.",
    );
    // The trailing full stop survives — only the address is consumed.
    expect(text.endsWith(".")).toBe(true);
  });

  it.each([
    "http://localhost:8000/",
    "http://127.0.0.1:8000/index.html",
    "http://0.0.0.0:3000",
    "https://localhost:5173/app?x=1",
    "http://[::1]:8080/",
  ])("rewrites every loopback alias and port: %s", (url) => {
    const { text, replaced } = scrubSandboxUrls(`Open ${url} to see it.`);
    expect(replaced).toBe(true);
    expect(text).toBe("Open the Preview tab to see it.");
  });

  it("collapses the markdown wrappers instead of leaving a husk", () => {
    expect(scrubSandboxUrls("Visit [the site](http://localhost:8000/) now.").text).toBe(
      "Visit the Preview tab now.",
    );
    expect(scrubSandboxUrls("Visit <http://localhost:8000/> now.").text).toBe(
      "Visit the Preview tab now.",
    );
    expect(scrubSandboxUrls("Visit `http://127.0.0.1:8000` now.").text).toBe(
      "Visit the Preview tab now.",
    );
  });

  it("rewrites every occurrence, not just the first", () => {
    const { text } = scrubSandboxUrls(
      "Served at http://localhost:8000/ and the API at http://127.0.0.1:8001/api.",
    );
    expect(text).not.toMatch(/localhost|127\.0\.0\.1/);
  });

  it("leaves a real, reachable address alone", () => {
    const summary = "The docs are at https://example.com/docs and the repo at http://acme.dev:8000/.";
    const { text, replaced } = scrubSandboxUrls(summary);
    expect(replaced).toBe(false);
    expect(text).toBe(summary);
  });

  it("is a no-op on a summary with no addresses at all", () => {
    const summary = "The signup page is complete and correct.";
    expect(scrubSandboxUrls(summary)).toEqual({ text: summary, replaced: false });
  });
});
