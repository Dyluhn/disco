/**
 * BP-15 — ActivityFeed screenshot thumbnails: render when screenshot_path
 * present; onError swaps to placeholder; no img when absent.
 */

import { render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ActivityFeed } from "@/components/build/ActivityFeed";
import type { ActivityItem } from "@/lib/buildTrace";

const BASE_ITEM: ActivityItem = {
  id: "act-1",
  kind: "action",
  label: "Opened a page",
  status: "done",
  attention: false,
  expandable: {
    tool_name: "browser",
    arguments: { action: "navigate", url: "https://example.com" },
    output: "Page loaded",
  },
};

const AUTO_APPROVED_ITEM: ActivityItem = {
  ...BASE_ITEM,
  id: "act-auto",
  label: "Ran a command",
  autoApproved: true,
};

const ITEM_WITH_SCREENSHOT: ActivityItem = {
  ...BASE_ITEM,
  expandable: {
    ...BASE_ITEM.expandable!,
    screenshot_path: ".pmx/screenshots/0001-navigate.png",
  },
};

const CID = "conv_test123";

describe("ActivityFeed — screenshot thumbnails (BP-15)", () => {
  it("renders thumbnail img when screenshot_path present and conversationId given", () => {
    render(<ActivityFeed items={[ITEM_WITH_SCREENSHOT]} conversationId={CID} />);
    const img = screen.getByRole("img", { name: /screenshot/i });
    expect(img).toBeInTheDocument();
    expect((img as HTMLImageElement).src).toContain(
      `/conversations/${CID}/workspace/.pmx/screenshots/0001-navigate.png`,
    );
  });

  it("wraps thumbnail in an <a> that opens in a new tab", () => {
    render(<ActivityFeed items={[ITEM_WITH_SCREENSHOT]} conversationId={CID} />);
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noreferrer");
  });

  it("swaps to placeholder text on img error (suspended sandbox)", () => {
    render(<ActivityFeed items={[ITEM_WITH_SCREENSHOT]} conversationId={CID} />);
    const img = screen.getByRole("img", { name: /screenshot/i });
    fireEvent.error(img);
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(screen.getByText(/screenshot no longer available/i)).toBeInTheDocument();
  });

  it("renders no thumbnail when screenshot_path absent", () => {
    render(<ActivityFeed items={[BASE_ITEM]} conversationId={CID} />);
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("renders no thumbnail when conversationId not provided", () => {
    render(<ActivityFeed items={[ITEM_WITH_SCREENSHOT]} />);
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });
});

describe("ActivityFeed — auto · sandboxed badge (DC-03)", () => {
  it("renders 'auto · sandboxed' badge when autoApproved is true", () => {
    render(<ActivityFeed items={[AUTO_APPROVED_ITEM]} />);
    expect(screen.getByText(/auto · sandboxed/i)).toBeInTheDocument();
  });

  it("does NOT render badge when autoApproved is absent", () => {
    render(<ActivityFeed items={[BASE_ITEM]} />);
    expect(screen.queryByText(/auto · sandboxed/i)).not.toBeInTheDocument();
  });

  it("does NOT render badge when autoApproved is false", () => {
    const item: ActivityItem = { ...BASE_ITEM, autoApproved: false };
    render(<ActivityFeed items={[item]} />);
    expect(screen.queryByText(/auto · sandboxed/i)).not.toBeInTheDocument();
  });
});
