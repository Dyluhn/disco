/**
 * TieredSourcePanel — the one line that speaks for the rows the panel never
 * received (F7 item 5).
 *
 * The report event is capped at 1 MiB and `_report_event.fit_report_event`
 * trims raw evidence to fit, recording the total it started from. The panel
 * counted the rows it was handed, so lane T2's exhaustive proof listed its
 * sources and said nothing about the 1,094 discovery rows that were dropped.
 *
 * Run: npx vitest run src/components/research/TieredSourcePanel.test.tsx
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TieredSourcePanel } from "./TieredSourcePanel";
import type { SourceTiers } from "@/lib/deepResearchTrace";

const TIERS: SourceTiers = {
  cited: [{ source_url: "https://a.example/paper", source_title: "A paper" }],
  reviewed: [{ url: "https://b.example/blog", title: "A blog", status: "ok" }],
  discovered: [{ url: "https://c.example/dead", title: "Dead", status: "error" }],
  notKept: null,
};

describe("TieredSourcePanel — the sources it was not given", () => {
  it("says how many the run found and how many the report kept", () => {
    // The real numbers off T2's exhaustive report event.
    render(
      <TieredSourcePanel tiers={{ ...TIERS, notKept: { found: 1838, kept: 744 } }} />,
    );
    expect(
      screen.getByText(
        /1,838 sources found · 744 sources kept with this report — the rest were trimmed to fit it\./,
      ),
    ).toBeInTheDocument();
  });

  it("adds no line when the report carried its whole corpus", () => {
    render(<TieredSourcePanel tiers={TIERS} />);
    expect(screen.queryByText(/kept with this report/)).not.toBeInTheDocument();
  });

  it("still lists the tiers it does have", () => {
    render(
      <TieredSourcePanel tiers={{ ...TIERS, notKept: { found: 1838, kept: 744 } }} />,
    );
    expect(screen.getByRole("tab", { name: /Cited/ })).toBeInTheDocument();
    expect(screen.getByText("A paper")).toBeInTheDocument();
  });
});
