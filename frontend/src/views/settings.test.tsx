import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";
import { SettingsView } from "./SettingsView";

function withQuery(ui: ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("Settings — model-assignment matrix", () => {
  it("shows the absolute/manual story with a default primary + every role, cost-legible", async () => {
    withQuery(<SettingsView />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Choose model for Default primary/i })).toBeInTheDocument(),
    );
    // The absolute, no-automatic-routing story is stated.
    expect(screen.getByText(/no automatic routing/i)).toBeInTheDocument();
    // Every non-driver role has its own selector.
    for (const role of ["RAG answerer", "Query rewriter", "Summarizer", "NLI verifier"]) {
      expect(
        screen.getByRole("button", { name: new RegExp(`Choose model for ${role}`, "i") }),
      ).toBeInTheDocument();
    }
    // Cost is visible per assignment (all local → Free).
    expect(screen.getAllByText("Free").length).toBeGreaterThan(0);
  });

  it("changes a role assignment (the picker is enabled and the row reflects it)", async () => {
    // Assignments are now wired end-to-end (ConfigStore -> agent-server routing),
    // so the picker is operable, not a flagged dead control.
    const user = userEvent.setup();
    withQuery(<SettingsView />);
    const ragTrigger = await screen.findByRole("button", {
      name: /Choose model for RAG answerer/i,
    });
    expect(ragTrigger).toBeEnabled();
    expect(ragTrigger).toHaveTextContent(/Rag Local/i);

    await user.click(ragTrigger);
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByText(/Summarizer Local/i));

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /Choose model for RAG answerer/i }),
      ).toHaveTextContent(/Summarizer Local/i),
    );
  });
});

describe("Settings — model catalogue (CRUD)", () => {
  it("adds a new model via the form and it appears in the catalogue", async () => {
    const user = userEvent.setup();
    withQuery(<SettingsView />);
    await screen.findByText("Catalogue");

    await user.click(screen.getByRole("button", { name: /Add model/i }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByPlaceholderText("my-llama"), "test-model");
    await user.type(within(dialog).getByPlaceholderText(/llama-3.3-70b/i), "test.gguf");
    await user.click(within(dialog).getByRole("button", { name: /^Add model$/i }));

    // the new model shows in the catalogue list (label derived like the backend)
    await waitFor(() =>
      expect(screen.getByText(/Test Model — test/i)).toBeInTheDocument(),
    );
  });
});

describe("Settings — OpenRouter", () => {
  it("saves the key and adds an OpenRouter model to the catalogue", async () => {
    const user = userEvent.setup();
    withQuery(<SettingsView />);
    await screen.findByText("OpenRouter");

    // save the (encrypted) key (the input appears once the key-status query resolves)
    await user.type(await screen.findByLabelText(/OpenRouter API key/i), "sk-or-v1-test");
    await user.click(screen.getByRole("button", { name: /Save key/i }));
    await waitFor(() => expect(screen.getByText(/Key configured/i)).toBeInTheDocument());

    // browse the catalogue and add a model
    await user.click(screen.getByRole("button", { name: /Browse OpenRouter models/i }));
    const dialog = await screen.findByRole("dialog");
    // claude is already "Added" (the seed overflow model maps to it), so the first
    // Add button is the next model (gpt-4o) — proving the added-detection works.
    const adds = await within(dialog).findAllByRole("button", { name: /^Add$/i });
    await user.click(adds[0]);

    // it lands in our catalogue (namespaced id), assignable like any other model
    await waitFor(() => expect(screen.getByText(/or-openai-gpt-4o/i)).toBeInTheDocument());
  });
});

describe("Settings — skills + MCP scaffolds", () => {
  it("skills are a real, persistent subsystem with a working create editor", async () => {
    window.localStorage.clear();
    const user = userEvent.setup();
    withQuery(<SettingsView />);
    // The Skills section renders with a working "New skill" button (not a
    // disabled, not-wired scaffold anymore).
    const newBtn = await screen.findByRole("button", { name: /New skill/i });
    expect(newBtn).toBeEnabled();
    // Clicking it opens a real editor with name / description / instructions.
    await user.click(newBtn);
    expect(
      await screen.findByRole("textbox", { name: /Skill name/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("textbox", { name: /Skill instructions/i }),
    ).toBeInTheDocument();
    // Save is gated until name + body are filled (honest affordance).
    expect(screen.getByRole("button", { name: /Save skill/i })).toBeDisabled();
  });

  it("lists MCP connections with an inert (pending) add affordance", async () => {
    withQuery(<SettingsView />);
    expect(await screen.findByText("Filesystem")).toBeInTheDocument();
    expect(screen.getByText("Connected")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Add connection/i })).toBeDisabled();
  });
});

describe("Settings — sandbox", () => {
  it("offers the three backends with isolation tiers legible; Podman is a stub", async () => {
    withQuery(<SettingsView />);
    expect(await screen.findByRole("heading", { name: "Sandbox" })).toBeInTheDocument();
    // radios render after the async config load
    expect(await screen.findByRole("radio", { name: /gVisor sandbox backend/i })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Local container sandbox backend/i })).toBeInTheDocument();
    const podman = screen.getByRole("radio", { name: /Podman .* sandbox backend/i });
    expect(within(podman).getByText(/stub here/i)).toBeInTheDocument();
    // isolation tiers are surfaced at the point of choice
    expect(screen.getByText(/Strong isolation/i)).toBeInTheDocument();
    expect(screen.getAllByText(/shared kernel/i).length).toBeGreaterThan(0);
  });

  it("surfaces the local → tighter-confirmation coupling when local is selected", async () => {
    const user = userEvent.setup();
    withQuery(<SettingsView />);
    await user.click(await screen.findByRole("radio", { name: /Local container sandbox backend/i }));
    expect(screen.getByText(/leans TIGHTER/i)).toBeInTheDocument();
  });

  it("saves a backend change (round-trips through the data layer)", async () => {
    const user = userEvent.setup();
    withQuery(<SettingsView />);
    const gvisor = await screen.findByRole("radio", { name: /gVisor sandbox backend/i });
    const local = screen.getByRole("radio", { name: /Local container sandbox backend/i });
    // pick whichever is NOT currently selected → the form is dirty → Save enabled
    const target = gvisor.getAttribute("aria-checked") === "true" ? local : gvisor;
    await user.click(target);
    const save = screen.getByRole("button", { name: /save sandbox/i });
    expect(save).toBeEnabled();
    await user.click(save);
    await waitFor(() => expect(screen.getByRole("button", { name: /^saved$/i })).toBeInTheDocument());
  });
});
