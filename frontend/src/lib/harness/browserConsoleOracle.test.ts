import { describe, expect, it } from "vitest";
import { expectedBrowserConsoleError } from "./browserConsoleOracle";

const FIREFOX_FAVICON_CSP =
  '[JavaScript Error: "Content-Security-Policy: The page’s settings blocked the loading of a resource (img-src) at http://preview.localhost/favicon.ico because it violates the following directive: “default-src \'none\'”" {file: "resource:///modules/FaviconLoader.sys.mjs" line: 224}]';
const FIREFOX_AGENT_WEBSOCKET =
  '[JavaScript Error: "Firefox can’t establish a connection to the server at ws://127.0.0.1:5302/svc/agent/ws/conversations/conv_123?last_seq=97." {file: "http://127.0.0.1:5302/src/api/agent.ts" line: 231}]';

describe("expectedBrowserConsoleError", () => {
  it("recognizes only Firefox's internal favicon CSP report", () => {
    expect(expectedBrowserConsoleError(FIREFOX_FAVICON_CSP, false)).toBe(
      "firefox-internal-favicon-csp",
    );
    expect(
      expectedBrowserConsoleError(
        FIREFOX_FAVICON_CSP.replace(
          "resource:///modules/FaviconLoader.sys.mjs",
          "http://preview.localhost/app.js",
        ),
        false,
      ),
    ).toBeNull();
  });

  it("recognizes agent websocket loss only during the planned restart", () => {
    expect(expectedBrowserConsoleError(FIREFOX_AGENT_WEBSOCKET, true)).toBe(
      "planned-agent-restart",
    );
    expect(expectedBrowserConsoleError(FIREFOX_AGENT_WEBSOCKET, false)).toBeNull();
  });

  it("does not hide other application console errors", () => {
    expect(expectedBrowserConsoleError("TypeError: application exploded", true)).toBeNull();
    expect(
      expectedBrowserConsoleError(
        FIREFOX_AGENT_WEBSOCKET.replace("/svc/agent/ws/conversations/", "/app/ws/"),
        true,
      ),
    ).toBeNull();
  });
});
