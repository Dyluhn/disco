/**
 * W-05-fu — BuildModelPicker shows a SUBSCRIPTION model as "subscription", never "free".
 *
 * A subscription model (flat-rate plan: price 0/token but NOT free) arrives from the
 * driver-models endpoint with pricing_mode === "subscription" and free === false. The
 * picker must surface its own "subscription" tag (and group) so it never reads as free.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useDriverModels", () => ({
  useDriverModels: () => ({
    data: {
      models: [
        { id: "local-free", label: "LocalFree Q4", provider: "local", free: true, pricing_mode: "free", context_window: 65536 },
        { id: "driver-minimax", label: "MiniMax M2", provider: "local", free: false, pricing_mode: "subscription", context_window: 200000 },
        { id: "or-paid", label: "PaidModel Sonnet", provider: "openrouter", free: false, pricing_mode: "metered", context_window: 200000 },
      ],
      default: "local-free",
    },
  }),
  useLastSelectedModel: vi.fn(),
}));

vi.mock("@/components/Toast", () => ({
  useToast: () => ({ show: vi.fn() }),
}));

import { BuildModelPicker } from "./BuildModelPicker";
import { useLastSelectedModel } from "@/hooks/useDriverModels";

const mockedUseLastSelectedModel = vi.mocked(useLastSelectedModel);

function withQuery(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("BuildModelPicker — W-05-fu subscription is not free", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedUseLastSelectedModel.mockReturnValue({ data: null } as ReturnType<typeof useLastSelectedModel>);
  });

  it("renders the subscription model's trigger tag as 'subscription', not 'free'", async () => {
    // The subscription model is the explicit value → it's the active trigger label.
    withQuery(<BuildModelPicker value="driver-minimax" onChange={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText(/MiniMax M2/i)).toBeInTheDocument();
    });
    const trigger = screen.getByLabelText(/Choose the model that runs the agent/i);
    expect(within(trigger).getByText(/subscription/i)).toBeInTheDocument();
    expect(within(trigger).queryByText(/·\s*free/i)).not.toBeInTheDocument();
  });

  it("tags free / subscription / paid distinctly on the trigger", async () => {
    // Free model → "· free".
    const free = withQuery(<BuildModelPicker value="local-free" onChange={() => {}} />);
    await waitFor(() => expect(screen.getByText(/LocalFree Q4/i)).toBeInTheDocument());
    let trigger = screen.getByLabelText(/Choose the model that runs the agent/i);
    expect(within(trigger).getByText(/·\s*free/i)).toBeInTheDocument();
    expect(within(trigger).queryByText(/·\s*subscription/i)).not.toBeInTheDocument();
    free.unmount();

    // Paid metered model → "· paid", never free.
    withQuery(<BuildModelPicker value="or-paid" onChange={() => {}} />);
    await waitFor(() => expect(screen.getByText(/PaidModel Sonnet/i)).toBeInTheDocument());
    trigger = screen.getByLabelText(/Choose the model that runs the agent/i);
    expect(within(trigger).getByText(/·\s*paid/i)).toBeInTheDocument();
    expect(within(trigger).queryByText(/·\s*free/i)).not.toBeInTheDocument();
  });
});
