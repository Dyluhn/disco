import { render, screen, waitFor } from "@testing-library/react";
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

  it("offers example-query pills that start a run", async () => {
    const user = userEvent.setup();
    render(<App />);
    // An outlined example pill (calm identity, not a marketing card).
    const pill = screen.getByRole("button", { name: /How RRF works/i });
    await user.click(pill);
    // Clicking it submits the query → the document title renders it.
    await waitFor(
      () =>
        expect(
          screen.getByRole("heading", { name: /reciprocal rank fusion/i }),
        ).toBeInTheDocument(),
      { timeout: 3000 },
    );
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
