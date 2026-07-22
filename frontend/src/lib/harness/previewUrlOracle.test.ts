import { describe, expect, it } from "vitest";

import {
  ISOLATED_PREVIEW_PREFIX,
  isIsolatedPreviewUrl,
  isStaticPreviewHostnameForCid,
} from "./previewUrlOracle";

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

  it.each(["8000", "5173"])(
    "binds a committed-static hostname to its CID at valid port %s",
    (port) => {
      expect(
        isStaticPreviewHostnameForCid(
          `p3s-deadbeef-${"a".repeat(40)}-${port}.localhost`,
          "conv_deadbeef12345678",
        ),
      ).toBe(true);
    },
  );

  it.each([
    `p3s-feedface-${"a".repeat(40)}-5173.localhost`,
    `p3s-deadbeef-${"a".repeat(39)}-5173.localhost`,
    `p3s-deadbeef-${"a".repeat(40)}-8.localhost`,
    "p2-deadbeef-5173.localhost",
  ])("rejects a static hostname not bound to the exact CID contract: %s", (hostname) => {
    expect(isStaticPreviewHostnameForCid(hostname, "conv_deadbeef12345678")).toBe(false);
  });
});
