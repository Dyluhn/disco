import { describe, expect, it } from "vitest";

import { ISOLATED_PREVIEW_PREFIX, isIsolatedPreviewUrl } from "./previewUrlOracle";

describe("isIsolatedPreviewUrl", () => {
  it.each([
    "http://p2-deadbeef-8000.localhost/?r=0",
    `https://p3s-deadbeef-${"a".repeat(40)}-8000.preview.example/docs/`,
    `http://127.0.0.1:18912${ISOLATED_PREVIEW_PREFIX}/conv_deadbeef/asset.js`,
  ])("recognizes every isolated preview response family: %s", (url) => {
    expect(isIsolatedPreviewUrl(url)).toBe(true);
  });

  it.each([
    "http://127.0.0.1:18912/ordinary-api",
    "http://p2-nothex-8000.localhost/",
    "http://p2-deadbeef-8.localhost/",
    `http://p3s-deadbeef-${"a".repeat(39)}-8000.localhost/`,
    "not a URL",
  ])("rejects non-preview and malformed lookalikes: %s", (url) => {
    expect(isIsolatedPreviewUrl(url)).toBe(false);
  });
});
