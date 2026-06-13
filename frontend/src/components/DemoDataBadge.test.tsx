/**
 * DemoDataBadge — unit tests.
 *
 * Two cases:
 *  A. env load failed / no backend URL → badge with "Demo data" text is present.
 *  B. backend URL configured          → badge is absent.
 *
 * We mock @/api/client so the module-level BASE/AGENT_BASE constants that are
 * evaluated at import time don't determine the outcome; each test controls
 * isDemoMode() independently via vi.hoisted (avoids TDZ errors with vi.mock hoisting).
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

// vi.hoisted runs before any imports are resolved, so the reference is safe
// inside the vi.mock factory below.
const { isDemoModeMock } = vi.hoisted(() => ({
  isDemoModeMock: vi.fn<[], boolean>(),
}));

vi.mock("@/api/client", () => ({
  isDemoMode: isDemoModeMock,
  isLive: vi.fn(() => false),
  agentLive: vi.fn(() => false),
}));

import { DemoDataBadge } from "./DemoDataBadge";

describe("DemoDataBadge", () => {
  it("renders the badge when isDemoMode() is true (env load failed / no backend)", () => {
    isDemoModeMock.mockReturnValue(true);
    render(<DemoDataBadge />);

    // Primary assertion: text matching /demo data/i is visible.
    expect(screen.getByText(/demo data/i)).toBeInTheDocument();

    // Secondary: the role="status" container with the accessible label is there.
    expect(
      screen.getByRole("status", { name: /no backend connected/i }),
    ).toBeInTheDocument();
  });

  it("renders nothing when isDemoMode() is false (backend URL present)", () => {
    isDemoModeMock.mockReturnValue(false);
    render(<DemoDataBadge />);

    // Badge must be completely absent — not hidden, not aria-hidden, literally not mounted.
    expect(screen.queryByText(/demo data/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("status", { name: /no backend connected/i })).not.toBeInTheDocument();
  });
});
