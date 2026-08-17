import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { streamTiming } from "@/api/research";
import App from "@/App";

const SURFACE_LOAD_TIMEOUT = 10_000;
const TEST_TIMEOUT = 15_000;

describe("Main surface — empty state + reactive provider errors", () => {
  beforeEach(() => {
    streamTiming.ttft = 0;
    streamTiming.token = 0;
    streamTiming.blockGap = 0;
  });

  it("offers suggestion chips that fill the composer", async () => {
    const user = userEvent.setup();
    render(<App />);
    const input = await screen.findByPlaceholderText(
      /ask anything/i,
      {},
      { timeout: SURFACE_LOAD_TIMEOUT },
    );
    const pills = screen
      .getAllByRole("button")
      .filter((button) => button.dataset.discoControl === "search.suggestion");
    expect(pills.length).toBeGreaterThanOrEqual(4);
    expect(pills.length).toBeLessThanOrEqual(6);

    const pillText = pills[0].textContent?.replace(/\s+/g, " ").trim() ?? "";
    await user.click(pills[0]);
    expect(input).toHaveValue(pillText);
  }, TEST_TIMEOUT);

  it("renders distinct landing titles and descriptors per surface", async () => {
    const user = userEvent.setup();
    render(<App />);

    expect(
      await screen.findByRole(
        "heading",
        { level: 1, name: "Disco" },
        { timeout: SURFACE_LOAD_TIMEOUT },
      ),
    ).toBeInTheDocument();
    expect(
      await screen.findByText(
        "Sourced answers on anything.",
        {},
        { timeout: SURFACE_LOAD_TIMEOUT },
      ),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("radio", { name: "build" }));
    expect(
      await screen.findByText(
        "Real software, live preview.",
        {},
        { timeout: SURFACE_LOAD_TIMEOUT },
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Disco" })).toBeInTheDocument();

    await user.click(screen.getByRole("radio", { name: "agent" }));
    expect(
      await screen.findByText(
        "Hands-on tasks and workflows.",
        {},
        { timeout: SURFACE_LOAD_TIMEOUT },
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByPlaceholderText("Describe a task for the agent to carry out…"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Import project" })).toBeNull();

    await user.click(screen.getByRole("radio", { name: "search" }));
    await user.click(
      await screen.findByRole(
        "button",
        { name: /Scope: Standard/i },
        { timeout: SURFACE_LOAD_TIMEOUT },
      ),
    );
    await user.click(screen.getByRole("menuitem", { name: /Deep Research/i }));
    expect(
      await screen.findByText(
        "Deep Research — Multi-step reports with cited evidence.",
        {},
        { timeout: SURFACE_LOAD_TIMEOUT },
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Disco" })).toBeInTheDocument();
  }, TEST_TIMEOUT);

  it("surfaces a provider error with its REAL content, not a generic failure", async () => {
    const user = userEvent.setup();
    render(<App />);
    await user.type(
      await screen.findByPlaceholderText(/ask anything/i, {}, { timeout: SURFACE_LOAD_TIMEOUT }),
      "provider-error please",
    );
    await user.keyboard("{Enter}");

    const alert = await screen.findByRole("alert", {}, { timeout: 3000 });
    // The real provider message is shown verbatim (typed class + reason)…
    expect(alert).toHaveTextContent("LLMContentFiltered");
    expect(alert).toHaveTextContent(/image input is not supported/i);
    // …and it is NOT flattened into a generic message.
    expect(alert).not.toHaveTextContent(/something went wrong/i);
    expect(alert).toHaveTextContent(/the model returned an error/i);
  }, TEST_TIMEOUT);
});
