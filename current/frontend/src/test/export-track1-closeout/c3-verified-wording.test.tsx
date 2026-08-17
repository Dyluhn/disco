/**
 * WO-C3 §7.9 — "Ready to self-host" wording is PRESERVED for verification.
 *
 * FROZEN acceptance path (plan §1.1). Locked semantics §2.2 + plan §7 (WO-C3)
 * acceptance 9: `verified` fixture data may render "Ready to self-host", proving the
 * wording is tied to VERIFICATION rather than deleted indiscriminately when the
 * §7.7 fix stops a `candidate` from claiming "Ready".
 *
 * `"verified"` IS expressible in the current type: `ReleaseAssessmentState` already
 * includes the verification-lifecycle values (`verifying` | `verified` | `failed`)
 * for forward-compatibility (see `current/frontend/src/types/release.ts`), so this renders a
 * genuine `verified` `ReleaseResponse` at the real component boundary (the `release`
 * prop) — NOT a mock, and with no `any`-cast to force the state.
 *
 * Boundary: there is no verifier and no offline `verified` fixture/hook path (§15
 * non-goal), so per the house rule a test-local `ReleaseResponse` is passed as the
 * real component's `release` prop — the honest component boundary for a state that
 * has no backend path yet.
 *
 * RED/GREEN on baseline `2ec1ceba`: GREEN-PRESERVATION. Baseline `SelfHostPanel`
 * renders "Ready to self-host" whenever `self_host` is true, so a verified verdict
 * (self_host:true) shows it today. This test pins that the wording SURVIVES the C3
 * fix. The teeth that the wording must become verification-GATED (a `candidate` with
 * self_host:true must NOT say "Ready") are the §7.7 red (c3-candidate-copy);
 * together they encode "Ready ⇔ verified", the only self_host:true non-verified
 * state being `candidate` (blockers force self_host:false — §2.3/§7.6).
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ReleaseResponse } from "@/types/release";
import { SelfHostPanel } from "@/components/build/SelfHostPanel";

/** A verified release: verification has established a runnable, self-hostable
 * project — the one state for which "Ready to self-host" is honest (§2.2). */
function verified(): ReleaseResponse {
  return {
    assessment: "verified",
    reasons: ["Verification confirmed the project boots and serves on $PORT."],
    blockers: [],
    required_env: [{ name: "PORT", scope: "runtime", required: true, secret: false }],
    command: "docker compose up -d --build",
    ingress: { service: "web", port: "PORT", health_path: "/healthz" },
    self_host: true,
    spec_digest: "sha256:demo-verified-0001",
    version_seq: 9,
    tree_digest: "sha256:tree-verified-0009",
  };
}

describe("WO-C3 §7.9 — 'Ready to self-host' is preserved for verified", () => {
  it("renders 'Ready to self-host' for a verified release", () => {
    const { container } = render(<SelfHostPanel release={verified()} onDownload={vi.fn()} />);

    // The honest readiness wording, reserved for verification (§2.2).
    expect(screen.getByText(/ready to self-host/i)).toBeInTheDocument();
    expect(container.querySelector('[data-self-host-status="ready"]')).not.toBeNull();
  });
});
