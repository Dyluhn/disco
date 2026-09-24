import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import type { AppKitOwnerControls, TweakValue } from "@/types/appkitOwner";
import { AppKitOwnerPanel } from "./AppKitOwnerPanel";
import * as api from "@/api/agentAppkitOwner";

vi.mock("@/api/agentAppkitOwner", () => ({ getAppKitControls: vi.fn(), saveAppKitControls: vi.fn() }));
let saved: AppKitOwnerControls;
beforeEach(() => {
  vi.resetAllMocks();
  saved = {
    title: "Orchard", spec_digest: "revision-one", editable: true,
    fields: [
      { key: "brand.name", label: "Brand name", editor: "text" },
      { key: "design.accent", label: "Accent palette", editor: "palette", colors: ["#246b55", "#265e91"] },
      { key: "design.text", label: "Text color", editor: "color" },
      { key: "layout.density", label: "Spacing", editor: "enum", options: ["compact", "airy"] },
      { key: "layout.width", label: "Content width", editor: "int", min: 640, max: 1440, step: 40 },
      { key: "motion.reduced", label: "Reduce motion", editor: "boolean" },
    ],
    values: { "brand.name": "Orchard", "design.accent": "#246b55", "design.text": "#263238", "layout.density": "compact", "layout.width": 1080, "motion.reduced": false },
  };
  vi.mocked(api.getAppKitControls).mockImplementation(async () => ({ controls: saved }));
  vi.mocked(api.saveAppKitControls).mockImplementation(async (_cid, _digest, values) => {
    saved = { ...saved, values: { ...saved.values, ...values }, spec_digest: "revision-two" };
    return { controls: saved };
  });
});
function mount(status = "FINISHED") {
  return render(<QueryClientProvider client={new QueryClient()}><AppKitOwnerPanel cid="conv_app" status={status} /></QueryClientProvider>);
}
it("stages every editor, saves one typed patch, and reloads persisted values", async () => {
  const view = mount();
  await screen.findByLabelText("Brand name");
  await waitFor(() => expect(screen.getByLabelText("Brand name")).toHaveValue("Orchard"));
  fireEvent.change(screen.getByLabelText("Brand name"), { target: { value: "North Shore" } });
  fireEvent.click(screen.getByRole("button", { name: "#265e91" }));
  fireEvent.change(screen.getByLabelText("Text color"), { target: { value: "#112233" } });
  fireEvent.change(screen.getByLabelText("Spacing"), { target: { value: "airy" } });
  fireEvent.change(screen.getByLabelText("Content width"), { target: { value: "1280" } });
  fireEvent.click(screen.getByLabelText("Reduce motion"));
  expect(api.saveAppKitControls).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
  const changed: Record<string, TweakValue> = {
    "brand.name": "North Shore", "design.accent": "#265e91", "design.text": "#112233",
    "layout.density": "airy", "layout.width": 1280, "motion.reduced": true,
  };
  await waitFor(() => expect(api.saveAppKitControls).toHaveBeenCalledExactlyOnceWith("conv_app", "revision-one", changed));
  await waitFor(() => expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled());
  view.unmount(); mount();
  await waitFor(() => expect(screen.getByLabelText("Brand name")).toHaveValue("North Shore"));
  expect(screen.getByLabelText("Reduce motion")).toBeChecked();
  expect(screen.getByLabelText("Content width")).toHaveValue("1280");
  expect(api.saveAppKitControls).toHaveBeenCalledTimes(1);
});
it("keeps unsaved input after a failed revision and reloads authoritative values on request", async () => {
  vi.mocked(api.saveAppKitControls).mockRejectedValue(new Error("This app changed; reload controls."));
  mount();
  await waitFor(() => expect(screen.getByLabelText("Brand name")).toHaveValue("Orchard"));
  fireEvent.change(screen.getByLabelText("Brand name"), { target: { value: "Unsaved" } });
  fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
  await screen.findByRole("alert");
  expect(screen.getByLabelText("Brand name")).toHaveValue("Unsaved");
  saved = { ...saved, spec_digest: "external-edit", values: { ...saved.values, "brand.name": "New server value" } };
  fireEvent.click(screen.getByRole("button", { name: "Reload controls" }));
  await waitFor(() => expect(screen.getByLabelText("Brand name")).toHaveValue("New server value"));
});
it("disables all editor controls until the run finishes", async () => {
  saved = { ...saved, editable: false };
  mount("WAITING_FOR_CONFIRMATION");
  await screen.findByLabelText("Brand name");
  expect(screen.getByLabelText("Brand name")).toBeDisabled();
  expect(screen.getByLabelText("Reduce motion")).toBeDisabled();
  expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
  expect(api.saveAppKitControls).not.toHaveBeenCalled();
});
