/**
 * DriverOutageBanner — honest copy per HTTP status, verbatim sanitized provider
 * line in mono, and no interactive affordances (Resume/answer live elsewhere).
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DriverOutageBanner } from "./DriverOutageBanner";

const MESSAGE = "provider opencode-go returned HTTP 429 type=GoUsageLimitError";

describe("DriverOutageBanner", () => {
  it("names the usage/rate limit for HTTP 429 and shows the provider line verbatim", () => {
    render(
      <DriverOutageBanner
        outage={{ httpStatus: 429, message: MESSAGE, provider: "opencode-go", kind: "LLMTransientError" }}
      />,
    );
    expect(
      screen.getByText(
        "The model provider reported a usage/rate limit (HTTP 429) — the run paused after bounded retries. Resume when the provider's quota is available.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText(MESSAGE)).toBeInTheDocument();
  });

  it("names a plain provider outage for non-429 statuses", () => {
    render(
      <DriverOutageBanner
        outage={{
          httpStatus: 503,
          message: "provider fake returned HTTP 503 type=server_error",
          provider: "fake",
          kind: "LLMTransientError",
        }}
      />,
    );
    expect(
      screen.getByText(
        "The model provider is unavailable (HTTP 503) — the run paused after bounded retries.",
      ),
    ).toBeInTheDocument();
  });

  it("is non-interactive: renders no buttons or links (no false affordances)", () => {
    render(
      <DriverOutageBanner
        outage={{ httpStatus: 429, message: MESSAGE, provider: null, kind: null }}
      />,
    );
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByRole("link")).toBeNull();
  });
});
