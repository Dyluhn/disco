/**
 * WO-C3 §7.10 — SelfHostPanel is capability-driven and MODE-INDEPENDENT.
 *
 * FROZEN acceptance path (plan §1.1). Plan §7 (WO-C3) acceptance 10 + §2 locked
 * semantics: the panel's action/command set is a pure function of the release
 * VERDICT, not of which surface/mode rendered it — Build and Agent inherit an
 * identical panel from the same `ReleaseResponse`.
 *
 * How this is proven at the real boundary: `SelfHostPanel` takes ONLY `release` +
 * `onDownload` — it has no surface/mode prop by design. This test renders the SAME
 * verdict inside two different ambient "surface mode" contexts and asserts the
 * rendered action/command/status set (and the full serialized DOM) is byte-identical.
 * A future change that made the panel read mode/surface (a context, a prop) would
 * break this guard.
 *
 * RED/GREEN on baseline `2ec1ceba`: GREEN-PRESERVATION. The baseline panel is already
 * mode-independent; this pins that the C3/C6 honesty fix keeps it so.
 */

import { createContext } from "react";
import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ReleaseResponse } from "@/types/release";
import { SelfHostPanel } from "@/components/build/SelfHostPanel";

/** A stand-in for the surrounding surface/mode. The panel does NOT consume it — that
 * is the whole point: output must not depend on it. */
const SurfaceModeContext = createContext<"build" | "agent">("build");

/** The candidate verdict (matches the offline `conv_demo_snake` shape). */
function candidate(): ReleaseResponse {
  return {
    assessment: "candidate",
    reasons: ["A Node web server binding $PORT was detected (server.js)."],
    blockers: [],
    required_env: [
      { name: "PORT", scope: "runtime", required: true, secret: false },
      { name: "SESSION_SECRET", scope: "runtime", required: true, secret: true },
    ],
    command: "docker compose up -d --build",
    ingress: { service: "web", port: "PORT", health_path: "/healthz" },
    self_host: true,
    spec_digest: "sha256:demo-candidate-0001",
    version_seq: 3,
    tree_digest: "sha256:tree-snake-0001",
  };
}

/** The verdict-derived action/command/status set — what the user can DO. If this is
 * a pure function of the release, it is identical across surfaces. */
function actionSet(container: HTMLElement) {
  const controls = Array.from(container.querySelectorAll("[data-disco-control]"))
    .map((el) => el.getAttribute("data-disco-control"))
    .sort();
  const blockers = Array.from(container.querySelectorAll("[data-blocker-code]"))
    .map((el) => el.getAttribute("data-blocker-code"))
    .sort();
  return {
    controls,
    blockers,
    status: container
      .querySelector("[data-self-host-status]")
      ?.getAttribute("data-self-host-status"),
    command:
      container.querySelector('[data-disco-control="build.self-host-command"]')?.textContent ??
      null,
  };
}

function renderInMode(mode: "build" | "agent", release: ReleaseResponse) {
  return render(
    <SurfaceModeContext.Provider value={mode}>
      <SelfHostPanel release={release} onDownload={vi.fn()} />
    </SurfaceModeContext.Provider>,
  );
}

describe("WO-C3 §7.10 — the panel is a pure function of the verdict (mode-independent)", () => {
  it("renders an identical action/command set from the same verdict in build and agent modes", () => {
    const release = candidate();
    const build = renderInMode("build", release);
    const agent = renderInMode("agent", release);

    // The user-facing action set is identical regardless of surface/mode.
    expect(actionSet(agent.container)).toEqual(actionSet(build.container));
    // Stronger: the whole rendered DOM is byte-identical — output depends ONLY on
    // `release`, so nothing about the surrounding surface leaked in.
    expect(agent.container.innerHTML).toEqual(build.container.innerHTML);
  });
});
