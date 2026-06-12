/**
 * rp-11 residue — ActivityFeed sheet download: an honest <a download> to the
 * declared-artifact route, present only when a conversationId is available (no cid
 * → no false affordance), with subdir paths preserved via encodeURI.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ActivityFeed } from "@/components/build/ActivityFeed";
import type { ActivityItem } from "@/lib/buildTrace";

const CID = "conv_sheet1";

function itemWithSheet(filename: string): ActivityItem {
  return {
    id: "act-sheet",
    kind: "action",
    label: "Generated a workbook",
    status: "done",
    attention: false,
    expandable: {
      tool_name: "sheet_generate",
      arguments: { filename },
      sheet: { filename, title: "Q3 Budget", sheet_names: ["Summary", "Detail"] },
    },
  };
}

describe("ActivityFeed — sheet download (rp-11)", () => {
  it("renders a download link to the artifact route when conversationId is given", () => {
    render(<ActivityFeed items={[itemWithSheet("budget.xlsx")]} conversationId={CID} />);
    const link = screen.getByRole("link", { name: /Q3 Budget/i });
    expect(link).toHaveAttribute("download");
    expect(link.getAttribute("href")).toContain(`/conversations/${CID}/artifacts/budget.xlsx`);
    expect(screen.getByText(/2 sheets/i)).toBeInTheDocument();
  });

  it("preserves subdir slashes in the path (encodeURI, not encodeURIComponent)", () => {
    render(<ActivityFeed items={[itemWithSheet("reports/q4.xlsx")]} conversationId={CID} />);
    const link = screen.getByRole("link", { name: /Q3 Budget/i });
    expect(link.getAttribute("href")).toContain(
      `/conversations/${CID}/artifacts/reports/q4.xlsx`,
    );
  });

  it("renders NO download when conversationId is absent (no false affordance)", () => {
    render(<ActivityFeed items={[itemWithSheet("budget.xlsx")]} />);
    expect(screen.queryByRole("link", { name: /Q3 Budget/i })).not.toBeInTheDocument();
  });
});
