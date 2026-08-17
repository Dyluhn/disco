import { describe, expect, it } from "vitest";

import { decideVerification, type VerifyEvent } from "./verificationCapture";

const verifyAction = (id: string): VerifyEvent => ({ kind: "action", id, tool_call: { tool_name: "verify_web_app" } });
const verifyObs = (actionId: string, passed: boolean): VerifyEvent => ({
  kind: "observation",
  action_id: actionId,
  tool_result: { success: true, structured: { passed } },
});
const browserNavigate = (): VerifyEvent => ({
  kind: "action",
  tool_call: { tool_name: "browser", arguments: { action: "navigate", url: "http://localhost:8000/" } },
});
const status = (detail: string): VerifyEvent => ({ kind: "status", status: "FINISHED", detail });

describe("decideVerification (STAB-2b: mirror disco's actual finish-verification gate)", () => {
  it("verify_web_app with a structured pass → verified", () => {
    const ev = [verifyAction("a1"), verifyObs("a1", true)];
    expect(decideVerification(ev, "FINISHED")).toEqual({ ready_for_verification_called: true, passed: true });
  });

  it("browser-inspected + clean FINISHED (no verify_web_app) → verified (the run-2 case)", () => {
    const ev = [browserNavigate()];
    expect(decideVerification(ev, "FINISHED")).toEqual({ ready_for_verification_called: true, passed: true });
  });

  it("browser-inspected but disco released UNVERIFIED → NOT verified (fail-closed)", () => {
    const ev = [browserNavigate(), status("unverified_release")];
    expect(decideVerification(ev, "FINISHED")).toEqual({ ready_for_verification_called: true, passed: false });
  });

  it("browser-inspected but unverifiable_static_finish → NOT verified", () => {
    const ev = [browserNavigate(), status("unverifiable_static_finish")];
    expect(decideVerification(ev, "FINISHED")).toEqual({ ready_for_verification_called: true, passed: false });
  });

  it("no inspection at all + FINISHED → not ready, not passed", () => {
    expect(decideVerification([], "FINISHED")).toEqual({ ready_for_verification_called: false, passed: false });
  });

  it("verify_web_app CALLED but did not pass, no browser, clean FINISHED → ready but NOT passed", () => {
    // a failed verify cannot back-door to passed via the clean-terminal fallback (needs a browser inspection).
    const ev = [verifyAction("a1"), verifyObs("a1", false)];
    expect(decideVerification(ev, "FINISHED")).toEqual({ ready_for_verification_called: true, passed: false });
  });

  it("browser-inspected but the build did NOT reach a clean terminal (STUCK) → not passed", () => {
    expect(decideVerification([browserNavigate()], "STUCK")).toEqual({
      ready_for_verification_called: true,
      passed: false,
    });
  });

  it("the LAST verify_web_app verdict wins (verify→fix→re-verify)", () => {
    const ev = [verifyAction("a1"), verifyObs("a1", false), verifyAction("a2"), verifyObs("a2", true)];
    expect(decideVerification(ev, "FINISHED")).toEqual({ ready_for_verification_called: true, passed: true });
  });
});
