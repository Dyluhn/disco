/**
 * verificationCapture — derive the product-harness `verification` slice from a build's event log,
 * mirroring disco's ACTUAL finish-verification gate (P1B-LIVE-STABILITY STAB-2b).
 *
 * disco verifies a web deliverable at finish via EITHER the dedicated `verify_web_app` tool
 * (structured pass/fail verdict) OR a `browser` inspection of the served app (navigate + console
 * check) — and a build only reaches FINISHED once that gate is satisfied, UNLESS it took an
 * explicit unverified escape hatch, which disco stamps with a distinct StatusEvent detail
 * (`unverified_release` = the verify-cap released without a pass; `unverifiable_static_finish` =
 * verification was genuinely unavailable). The original harness recognized ONLY `verify_web_app`,
 * so a browser-verified build was falsely flagged VERIFICATION_GATE_BYPASSED.
 *
 * This is fail-closed: `passed` is true only when the build BOTH inspected its deliverable AND
 * disco accepted the finish as verified (clean FINISHED/VERIFIED, no unverified/unverifiable
 * release). It does NOT re-implement disco's console-error logic — it defers to disco's terminal
 * verdict (a build that rendered a broken page never finishes normally).
 */

export type VerifyEvent = {
  kind?: string;
  status?: string;
  detail?: string;
  action_id?: string;
  id?: string;
  tool_call?: { tool_name?: string; arguments?: Record<string, unknown> };
  tool_result?: { success?: boolean; structured?: { passed?: boolean } | null };
};

export type VerificationSlice = {
  ready_for_verification_called: boolean;
  passed: boolean;
};

const UNVERIFIED_DETAILS = new Set(["unverified_release", "unverifiable_static_finish"]);

export function decideVerification(events: VerifyEvent[], terminal: string): VerificationSlice {
  // (a) the dedicated verify tool — the strongest signal: a structured pass verdict.
  const verifyActions = events.filter((e) => e.tool_call?.tool_name === "verify_web_app");
  const verifyAction = verifyActions[verifyActions.length - 1]; // the FINAL finish-gate verify
  const verifyObs = verifyAction
    ? events.find((e) => e.kind === "observation" && e.action_id === verifyAction.id)
    : undefined;
  const verifyWebAppPassed =
    verifyObs?.tool_result?.success === true && verifyObs?.tool_result?.structured?.passed === true;

  // (b) browser-based inspection of the served app (the build looked at its deliverable).
  const browserInspected = events.some(
    (e) => e.tool_call?.tool_name === "browser" && e.tool_call?.arguments?.action === "navigate",
  );

  // disco's terminal verdict: did it finish via an explicit UNVERIFIED escape hatch?
  const unverifiedRelease = events.some(
    (e) => e.kind === "status" && UNVERIFIED_DETAILS.has(String(e.detail ?? "")),
  );
  const cleanTerminal = /^(FINISHED|VERIFIED)$/.test(terminal) && !unverifiedRelease;

  return {
    ready_for_verification_called: Boolean(verifyAction) || browserInspected,
    // verified IFF: a structured verify_web_app pass, OR the build BROWSER-inspected the deliverable
    // AND disco accepted the finish as verified (clean terminal, no unverified/unverifiable
    // release). The clean-terminal fallback requires a real browser inspection — a verify_web_app
    // that was CALLED but did not pass cannot back-door to passed=true.
    passed: verifyWebAppPassed || (browserInspected && cleanTerminal),
  };
}
