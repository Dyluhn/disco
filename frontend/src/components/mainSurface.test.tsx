import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { streamTiming } from "@/api/research";
import App from "@/App";

describe("Main surface — empty state + reactive provider errors", () => {
  beforeEach(() => {
    streamTiming.ttft = 0;
    streamTiming.token = 0;
    streamTiming.blockGap = 0;
  });

  it("offers suggestion chips that fill the composer", async () => {
    const user = userEvent.setup();
    render(<App />);
    const input = screen.getByPlaceholderText(/ask anything/i);
    const pills = screen
      .getAllByRole("button")
      .filter((button) => button.dataset.discoControl === "search.suggestion");
    expect(pills.length).toBeGreaterThanOrEqual(4);
    expect(pills.length).toBeLessThanOrEqual(6);

    const pillText = pills[0].textContent?.replace(/\s+/g, " ").trim() ?? "";
    await user.click(pills[0]);
    expect(input).toHaveValue(pillText);
  });

  it("renders distinct landing titles and descriptors per surface", async () => {
    const user = userEvent.setup();
    render(<App />);

    expect(screen.getByRole("heading", { level: 1, name: "Disco" })).toBeInTheDocument();
    expect(screen.getByText("Sourced answers on anything.")).toBeInTheDocument();

    await user.click(screen.getByRole("radio", { name: "build" }));
    expect(screen.getByRole("heading", { level: 1, name: "Disco" })).toBeInTheDocument();
    expect(screen.getByText("Real software, live preview.")).toBeInTheDocument();

    await user.click(screen.getByRole("radio", { name: "agent" }));
    expect(screen.getByText("Hands-on tasks and workflows.")).toBeInTheDocument();

    await user.click(screen.getByRole("radio", { name: "search" }));
    await user.click(screen.getByRole("button", { name: /Scope: Standard/i }));
    await user.click(screen.getByRole("menuitem", { name: /Deep Research/i }));
    expect(screen.getByRole("heading", { level: 1, name: "Disco" })).toBeInTheDocument();
    expect(
      screen.getByText("Deep Research — Multi-step reports with cited evidence."),
    ).toBeInTheDocument();
  });

  it("surfaces a provider error with its REAL content, not a generic failure", async () => {
    const user = userEvent.setup();
    render(<App />);
    await user.type(screen.getByPlaceholderText(/ask anything/i), "provider-error please");
    await user.keyboard("{Enter}");

    const alert = await screen.findByRole("alert", {}, { timeout: 3000 });
    // The real provider message is shown verbatim (typed class + reason)…
    expect(alert).toHaveTextContent("LLMContentFiltered");
    expect(alert).toHaveTextContent(/image input is not supported/i);
    // …and it is NOT flattened into a generic message.
    expect(alert).not.toHaveTextContent(/something went wrong/i);
    expect(alert).toHaveTextContent(/the model returned an error/i);
  });
});
