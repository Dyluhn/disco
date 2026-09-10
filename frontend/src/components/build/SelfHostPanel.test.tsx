/**
 * SelfHostPanel (WO-9) — capability-driven, mode-identical self-host handoff.
 *
 * The panel renders PURELY from a `ReleaseResponse` + an `onDownload` callback, so
 * these tests drive it with in-code fixtures of each Track-1 assessment and assert
 * the exact behaviour the work order pins:
 *   • candidate    → the exact run command, every required env NAME, a working
 *                    "Download source" trigger.
 *   • needs_review → each blocker message, and NO run command / no self-host-ready
 *                    affordance (no false affordance).
 *   • not_web      → the honest reason, and ONLY the "Download source" action.
 *   • secret hygiene → a secret env shows its NAME only, never a (fabricated) value.
 *   • no state renders a dead button (the one button is always wired).
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ReleaseResponse } from "@/types/release";
import { SelfHostPanel } from "@/components/build/SelfHostPanel";

const COMMAND = "docker compose up -d --build";

/** A self-hostable Node web app: `self_host` true, a run command, and two env
 * NAMES (one of them a secret). Mirrors the WO-8 fixture shape field-for-field. */
function candidate(): ReleaseResponse {
  return {
    assessment: "candidate",
    reasons: ["A Node web server binding $PORT was detected (server.js)."],
    blockers: [],
    required_env: [
      { name: "PORT", scope: "runtime", required: true, secret: false },
      { name: "SESSION_SECRET", scope: "runtime", required: true, secret: true },
    ],
    command: COMMAND,
    ingress: { service: "web", port: "PORT", health_path: "/healthz" },
    self_host: true,
    spec_digest: "sha256:demo-candidate-0001",
    version_seq: 3,
    tree_digest: "sha256:tree-snake-0001",
  };
}

/** Ambiguous topology: not self-hostable, reported as repairable blockers. */
function needsReview(): ReleaseResponse {
  return {
    assessment: "needs_review",
    reasons: ["Multiple candidate entrypoints were found; the public ingress is ambiguous."],
    blockers: [
      {
        code: "release_field_unresolved",
        field: "ingress",
        message: "no single web entrypoint could be resolved — declare which service is public.",
        path: null,
      },
      {
        code: "port_conflict",
        field: null,
        message: "two services both bind $PORT — only one can be the public ingress.",
        path: null,
      },
    ],
    required_env: [{ name: "API_BASE_URL", scope: "build", required: false, secret: false }],
    command: COMMAND,
    ingress: null,
    self_host: false,
    spec_digest: null,
    version_seq: 5,
    tree_digest: "sha256:tree-landing-0002",
  };
}

/** Not a web app at all: no ingress, no env, self-host off. */
function notWeb(): ReleaseResponse {
  return {
    assessment: "not_web",
    reasons: ["No web server entrypoint was detected; this looks like a static bundle."],
    blockers: [],
    required_env: [],
    command: COMMAND,
    ingress: null,
    self_host: false,
    spec_digest: null,
    version_seq: 1,
    tree_digest: "sha256:tree-orphan-0003",
  };
}

describe("SelfHostPanel — candidate (self-hostable)", () => {
  it("renders the EXACT run command", () => {
    render(<SelfHostPanel release={candidate()} onDownload={vi.fn()} />);
    // Exact command text, verbatim.
    expect(screen.getByText(COMMAND)).toBeInTheDocument();
    expect(screen.getByText("docker compose up -d --build")).toBeInTheDocument();
  });

  it("renders every required env NAME", () => {
    render(<SelfHostPanel release={candidate()} onDownload={vi.fn()} />);
    const list = screen.getByRole("list", { name: /required environment variables/i });
    expect(list).toBeInTheDocument();
    expect(screen.getByText("PORT")).toBeInTheDocument();
    expect(screen.getByText("SESSION_SECRET")).toBeInTheDocument();
  });

  it("offers a working 'Download source' trigger", async () => {
    const onDownload = vi.fn();
    render(<SelfHostPanel release={candidate()} onDownload={onDownload} />);
    const btn = screen.getByRole("button", { name: "Download source" });
    expect(btn).toBeEnabled();
    await userEvent.click(btn);
    expect(onDownload).toHaveBeenCalledOnce();
  });

  // UI-16 regression: the card used to lead with the DETECTOR's words ("detected a
  // static ingress service from the immutable project contents. Not runtime-verified
  // — the self-host bundle is available but has not been run."). It must now lead
  // with what this is, what to do, and what "verified" would mean — and the detector
  // diagnostic must be demoted, not deleted.
  it("leads with plain language and demotes the detector diagnostic", () => {
    const { container } = render(<SelfHostPanel release={candidate()} onDownload={vi.fn()} />);

    const note = container.querySelector('[data-self-host-note="unverified"]');
    expect(note).not.toBeNull();
    const text = note?.textContent ?? "";
    // What it is + what to do.
    expect(text).toMatch(/can run on its own/i);
    expect(text).toMatch(/download the source below/i);
    expect(text).toMatch(/run the command shown/i);
    // What "verified" would mean, without the word.
    expect(text).toMatch(/hasn't started it yet/i);
    expect(text).toMatch(/hasn't checked that it works/i);
    expect(text).not.toMatch(/runtime-verified/i);

    // The detector's own sentence is preserved verbatim, behind a disclosure — it is
    // evidence for the curious, never the headline.
    const detail = container.querySelector("[data-self-host-detection-reason]");
    expect(detail).not.toBeNull();
    expect(detail?.textContent).toBe("A Node web server binding $PORT was detected (server.js).");
    expect(detail?.closest("details")).not.toBeNull();
  });
});

describe("SelfHostPanel — needs_review", () => {
  it("renders EACH blocker message", () => {
    render(<SelfHostPanel release={needsReview()} onDownload={vi.fn()} />);
    for (const b of needsReview().blockers) {
      expect(screen.getByText(b.message)).toBeInTheDocument();
    }
  });

  it("shows NO run command and NO self-host-ready affordance", () => {
    render(<SelfHostPanel release={needsReview()} onDownload={vi.fn()} />);
    // No command anywhere.
    expect(screen.queryByText(COMMAND)).not.toBeInTheDocument();
    // No self-host-command control element rendered.
    expect(
      document.querySelector('[data-disco-control="build.self-host-command"]'),
    ).toBeNull();
    // The status pill honestly says review is needed (not "ready").
    expect(screen.queryByText(/ready to self-host/i)).not.toBeInTheDocument();
    expect(screen.getByText(/needs review/i)).toBeInTheDocument();
  });
});

describe("SelfHostPanel — not_web", () => {
  it("renders the honest reason", () => {
    render(<SelfHostPanel release={notWeb()} onDownload={vi.fn()} />);
    expect(
      screen.getByText(/no web server entrypoint was detected/i),
    ).toBeInTheDocument();
  });

  it("offers ONLY the 'Download source' action (no command, no other buttons)", () => {
    render(<SelfHostPanel release={notWeb()} onDownload={vi.fn()} />);
    expect(screen.queryByText(COMMAND)).not.toBeInTheDocument();
    const buttons = screen.getAllByRole("button");
    expect(buttons).toHaveLength(1);
    expect(buttons[0]).toHaveAccessibleName("Download source");
  });
});

describe("SelfHostPanel — no dead buttons (no false affordance)", () => {
  it.each([
    ["candidate", candidate],
    ["needs_review", needsReview],
    ["not_web", notWeb],
  ])("every button is enabled and wired in the %s state", async (_name, make) => {
    const onDownload = vi.fn();
    render(<SelfHostPanel release={make()} onDownload={onDownload} />);
    const buttons = screen.getAllByRole("button");
    // No disabled/dead buttons in any state.
    for (const b of buttons) expect(b).toBeEnabled();
    // The download action is present and actually fires its handler.
    await userEvent.click(screen.getByRole("button", { name: "Download source" }));
    expect(onDownload).toHaveBeenCalledOnce();
  });
});

describe("SelfHostPanel — secret hygiene (NAMES only)", () => {
  it("renders a secret env's NAME but never a value", () => {
    const { container } = render(<SelfHostPanel release={candidate()} onDownload={vi.fn()} />);
    // The secret's NAME is shown.
    expect(screen.getByText("SESSION_SECRET")).toBeInTheDocument();
    // It is tagged as a secret (metadata word, not a value).
    expect(screen.getByText(/^secret$/i)).toBeInTheDocument();
    // No value-shaped content: nothing like NAME=value, and no fabricated
    // placeholder value for the secret env.
    const text = container.textContent ?? "";
    expect(text).not.toMatch(/SESSION_SECRET\s*[:=]/);
    expect(text).not.toMatch(/PORT\s*[:=]\s*\d/);
    // The type carries no value field, so there is nothing to leak — assert the
    // env list item exposes only the name via its data hook, no value attribute.
    const item = container.querySelector('[data-env-name="SESSION_SECRET"]');
    expect(item).not.toBeNull();
    expect(item?.getAttribute("data-env-value")).toBeNull();
  });
});
