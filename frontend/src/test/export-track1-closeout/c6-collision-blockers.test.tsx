/**
 * WO-C6 §10.4 — the UI renders EVERY overlay-collision blocker (code + the EXACT
 * colliding path) and offers NO self-host command / bound bundle action; the plain
 * source download remains.
 *
 * FROZEN acceptance path (plan §1.1). Plan §10 (WO-C6) acceptance 2 + 4: an overlay
 * path collision forces `needs_review`, `self_host:false`, `spec_digest:null`, and a
 * blocker `overlay_path_conflict` naming the EXACT path; the UI must render every
 * such collision blocker and expose no self-host-ready action.
 *
 * Boundary (house rule): there is no offline hook/fixture that produces an
 * `overlay_path_conflict` verdict (the collision detector is WO-C6 production work,
 * §15/§14), so a test-local `ReleaseResponse` is passed as the REAL `SelfHostPanel`'s
 * `release` prop — the honest component boundary for a state with no backend path
 * yet. `SelfHostPanel` is NOT mocked; no `any`-cast is used.
 *
 * The exact colliding path is carried in the blocker's structured `path` field and is
 * DELIBERATELY absent from the human `message`, so the assertion pins the UI contract
 * "surface the structured `blocker.path`" rather than being satisfied by incidental
 * message wording.
 *
 * WHY IT IS RED ON BASELINE `2ec1ceba`: `SelfHostPanel`'s review branch renders
 * `blocker.message` and stamps `data-blocker-code`, but never renders `blocker.path`.
 * So the exact colliding paths (`compose.yaml`, `Dockerfile`, `.env.example`) are
 * NOT visible in the DOM — the path-visibility assertions fail. WO-C6 must surface
 * the collision path.
 */

import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ReleaseResponse } from "@/types/release";
import { SelfHostPanel } from "@/components/build/SelfHostPanel";

const COLLIDING_PATHS = ["compose.yaml", "Dockerfile", ".env.example"] as const;

/** A needs_review verdict caused by overlay path collisions. Every generated overlay
 * file that collides is a separate `overlay_path_conflict` blocker naming the exact
 * workspace path it would clobber. The path lives ONLY in the structured `path`
 * field — never inlined into `message`. */
function collision(): ReleaseResponse {
  return {
    assessment: "needs_review",
    reasons: ["The generated self-host overlay collides with files already in the workspace."],
    blockers: COLLIDING_PATHS.map((path) => ({
      code: "overlay_path_conflict",
      field: null,
      path,
      message: "A generated self-host overlay file conflicts with an existing workspace file.",
    })),
    required_env: [],
    command: "docker compose up -d --build",
    ingress: null,
    self_host: false,
    spec_digest: null,
    version_seq: 7,
    tree_digest: "sha256:tree-collision-0007",
  };
}

describe("WO-C6 §10.4 — overlay collisions are rendered with the exact path, no self-host action", () => {
  it("renders every collision blocker (code + exact path) and only the plain download", () => {
    const { container } = render(<SelfHostPanel release={collision()} onDownload={vi.fn()} />);

    // Review state, and one rendered blocker per collision (code present).
    expect(container.querySelector('[data-self-host-status="needs_review"]')).not.toBeNull();
    expect(container.querySelectorAll('[data-blocker-code="overlay_path_conflict"]')).toHaveLength(
      COLLIDING_PATHS.length,
    );

    // No self-host-ready affordance in a collision: no run command...
    expect(container.querySelector('[data-disco-control="build.self-host-command"]')).toBeNull();
    expect(container.textContent ?? "").not.toMatch(/ready to self-host/i);
    // ...and the plain source download remains available.
    expect(container.querySelector('[data-disco-control="build.download-source"]')).not.toBeNull();

    // TEETH (§10.4 / §10.2): the EXACT colliding path of each blocker must be visible
    // so the user can see WHICH file conflicts. RED on baseline — the panel renders
    // only `blocker.message`, not the structured `blocker.path`.
    const domText = container.textContent ?? "";
    for (const path of COLLIDING_PATHS) {
      expect(domText).toContain(path);
    }
  });
});
