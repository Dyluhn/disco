export type ExpectedBrowserConsoleError =
  | "firefox-internal-favicon-csp"
  | "planned-agent-restart";

/**
 * Classify the two browser-internal errors that the manifest lifecycle test
 * deliberately provokes. Everything else remains a test failure.
 */
export function expectedBrowserConsoleError(
  message: string,
  plannedAgentRestart: boolean,
): ExpectedBrowserConsoleError | null {
  const firefoxInternalFaviconCsp =
    message.includes("Content-Security-Policy:") &&
    message.includes("(img-src)") &&
    message.includes("/favicon.ico") &&
    message.includes("default-src 'none'") &&
    message.includes("resource:///modules/FaviconLoader.sys.mjs");
  if (firefoxInternalFaviconCsp) return "firefox-internal-favicon-csp";

  const plannedAgentWebsocketDisconnect =
    plannedAgentRestart &&
    message.includes(
      "Firefox can’t establish a connection to the server at ws://127.0.0.1:5302/svc/agent/ws/conversations/",
    ) &&
    message.includes('{file: "http://127.0.0.1:5302/src/api/agent.ts" line:');
  if (plannedAgentWebsocketDisconnect) return "planned-agent-restart";

  return null;
}
