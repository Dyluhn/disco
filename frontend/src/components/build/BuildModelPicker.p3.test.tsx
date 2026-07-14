/**
 * P3 — BuildModelPicker defaults to last-selected model (server-side sticky pick).
 *
 * The pill reads useLastSelectedModel() from the agent-server. When a last-picked
 * model is returned, effectiveId = value ?? lastSelected ?? defaultId, so the pill
 * shows the last-selected label rather than the static settings default.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

// --- mock hooks so the component works in jsdom without a real backend -------

vi.mock("@/hooks/useDriverModels", () => ({
  useDriverModels: () => ({
    data: {
      models: [
        { id: "local-default", label: "LocalDefault Q4", provider: "local", free: true, context_window: 65536 },
        { id: "or-paid-model", label: "PaidModel Sonnet", provider: "openrouter", free: false, context_window: 200000 },
      ],
      default: "local-default",
    },
  }),
  useLastSelectedModel: vi.fn(),
}));

vi.mock("@/components/toastApi", () => ({
  useToast: () => ({ show: vi.fn() }),
}));

import { BuildModelPicker } from "./BuildModelPicker";
import { useLastSelectedModel } from "@/hooks/useDriverModels";

const mockedUseLastSelectedModel = vi.mocked(useLastSelectedModel);

function withQuery(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("BuildModelPicker — P3 sticky last-selected model", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows the static settings default when no last-selected model exists", async () => {
    mockedUseLastSelectedModel.mockReturnValue({ data: null } as ReturnType<typeof useLastSelectedModel>);

    withQuery(<BuildModelPicker value={null} onChange={() => {}} />);

    await waitFor(() => {
      // Should show the static default "local-default"
      expect(screen.getByText(/LocalDefault Q4/i)).toBeInTheDocument();
    });
  });

  it("shows the last-selected model label when a last-picked model is persisted", async () => {
    mockedUseLastSelectedModel.mockReturnValue({ data: "or-paid-model" } as ReturnType<typeof useLastSelectedModel>);

    withQuery(<BuildModelPicker value={null} onChange={() => {}} />);

    await waitFor(() => {
      // Should show the last-selected "or-paid-model"'s label
      expect(screen.getByText(/PaidModel Sonnet/i)).toBeInTheDocument();
    });
  });

  it("explicit value overrides last-selected (explicit pick always wins)", async () => {
    mockedUseLastSelectedModel.mockReturnValue({ data: "or-paid-model" } as ReturnType<typeof useLastSelectedModel>);

    // Explicit value = "local-default" (the user chose it for this conversation)
    withQuery(<BuildModelPicker value="local-default" onChange={() => {}} />);

    await waitFor(() => {
      expect(screen.getByText(/LocalDefault Q4/i)).toBeInTheDocument();
    });
    // PaidModel should NOT be displayed as the active label
    // (it may appear in the dropdown but not as the trigger label)
  });
});
