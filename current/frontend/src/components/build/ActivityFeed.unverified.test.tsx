/**
 * UI-12 — the unverified-release warning, as the USER sees it.
 *
 * The loop's ENVIRONMENT message is written at the MODEL ("… Note this clearly
 * in your summary.") and names internal probes. It has to keep working as a
 * prompt, so the feed rewrites it at render time: a plain headline + whether
 * the deliverable is still usable, with the raw sentence kept verbatim behind a
 * disclosure. It must NOT claim the run is "Finished" — the same card is on
 * screen while the status chip still says Working.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ActivityFeed } from "@/components/build/ActivityFeed";
import { deriveActivity } from "@/lib/buildTrace";
import { unverifiedWarning } from "@/lib/unverifiedWarning";
import type { AgentEvent } from "@/types/agent";

const RAW =
  "⚠ Finished WITHOUT a passing verify_web_app verdict — the deliverable is " +
  "UNVERIFIED and may be INCOMPLETE. trusted components: database-kit probe: " +
  "3/4 checks passed. — health_migration_version: body.migration_version = None " +
  "— expected a non-negative integer. Note this clearly in your summary.";

function envMessage(content: string): AgentEvent {
  return {
    id: "env-1",
    kind: "message",
    source: "environment",
    message: { role: "user", content },
  } as AgentEvent;
}

describe("unverifiedWarning (UI-12)", () => {
  it("names the check in plain words and says the work may still be usable", () => {
    const w = unverifiedWarning(RAW);
    expect(w).not.toBeNull();
    expect(w!.headline).toBe("One automatic check didn't pass");
    expect(w!.body).toContain("browser check");
    expect(w!.body).toMatch(/may still work/i);
    expect(w!.raw).toBe(RAW);
  });

  it("handles the host-verifier variant", () => {
    const w = unverifiedWarning(
      "⚠ Finished WITHOUT a passing host verifier verdict — the deliverable is " +
        "UNVERIFIED and may be INCOMPLETE. host verifier did not pass. " +
        "Note this clearly in your summary.",
    );
    expect(w!.body).toContain("host check");
  });

  it("leaves other ⚠ environment lines alone", () => {
    expect(unverifiedWarning("⚠ AppKit ejected to Freeform at v3")).toBeNull();
  });
});

describe("ActivityFeed — unverified-release warning card (UI-12)", () => {
  it("renders the plain sentence, not the model-facing instruction", () => {
    const items = deriveActivity([envMessage(RAW)], null, "RUNNING");
    expect(items).toEqual([expect.objectContaining({ kind: "system_warning" })]);

    render(<ActivityFeed items={items} />);
    const card = screen.getByTestId("unverified-warning");
    expect(screen.getByText("One automatic check didn't pass")).toBeInTheDocument();
    // The instruction to the model, the internal probe names and the "Finished"
    // that contradicts the status chip are all out of the card's own copy —
    // they survive only inside the (closed) disclosure below it.
    const disclosure = screen.getByText("Raw check output").closest("details")!;
    const spoken = [...card.querySelectorAll("p")].filter(
      (p) => !disclosure.contains(p),
    );
    const copy = spoken.map((p) => p.textContent).join(" ");
    expect(copy).not.toMatch(/note this clearly in your summary/i);
    expect(copy).not.toMatch(/health_migration_version/);
    expect(copy).not.toMatch(/⚠ Finished WITHOUT/);
    expect(copy).not.toMatch(/verify_web_app/);
  });

  it("keeps the raw text verbatim behind a disclosure — nothing is lost", () => {
    render(<ActivityFeed items={deriveActivity([envMessage(RAW)], null, "RUNNING")} />);
    const details = screen.getByText("Raw check output").closest("details")!;
    expect(details).not.toBeNull();
    expect(details.open).toBe(false);
    expect(details.textContent).toContain("health_migration_version");
    expect(details.textContent).toContain("Note this clearly in your summary.");
  });

  it("an unrelated ⚠ warning still renders its own text", () => {
    const items = deriveActivity(
      [envMessage("⚠ The sandbox ran out of disk space")],
      null,
      "RUNNING",
    );
    render(<ActivityFeed items={items} />);
    expect(screen.getByText("⚠ The sandbox ran out of disk space")).toBeInTheDocument();
    expect(screen.queryByTestId("unverified-warning")).toBeNull();
  });
});
