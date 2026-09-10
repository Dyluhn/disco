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

describe("Settings — attention banner", () => {
  it("presents an unconfigured optional feature as informational, not an error", async () => {
    // UI-2: image generation nobody has turned on rendered as a red error
    // banner, both in its own section and at the top of the page, so a stock
    // install read as a list of faults the user had caused.
    withQuery(<SettingsView />);
    const title = await screen.findByText(/Image generation — optional, not set up/i);
    const row = title.closest("[data-attention-tone]");
    expect(row).toHaveAttribute("data-attention-tone", "optional");
    expect(row?.className).not.toMatch(/warn|unsupported/);
  });
});

describe("Settings — model-assignment matrix", () => {
  it("shows the absolute/manual story with a default primary + every role, cost-legible", async () => {
    const user = userEvent.setup();
    withQuery(<SettingsView />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Choose model for Default primary/i })).toBeInTheDocument(),
    );
    expect(
      screen.getByRole("button", {
        name: /Choose optional visual inspection model/i,
      }),
    ).toBeInTheDocument();
    // The manual-assignment story is stated without competing catalogue language.
    expect(screen.getByText(/Assignments are manual/i)).toBeInTheDocument();
    // Specialist roles do not crowd the normal setup path.
    const specialistSummary = screen.getByText(/Specialist role overrides/i, {
      selector: "summary",
    });
    expect(specialistSummary.closest("details")).not.toHaveAttribute("open");
    await user.click(specialistSummary);
    expect(specialistSummary.closest("details")).toHaveAttribute("open");

    // Every GENERATIVE non-driver role remains explicitly assignable.
    for (const role of ["RAG answerer", "Query rewriter", "Summarizer"]) {
      expect(
        screen.getByRole("button", { name: new RegExp(`Choose model for ${role}`, "i") }),
      ).toBeInTheDocument();
    }
    // ...but NLI is an ENCODER (Settings → Encoders), NOT an assignable LLM role:
    // surfacing it in the matrix would be a false affordance (the assignment is ignored).
    expect(
      screen.queryByRole("button", { name: /Choose model for NLI verifier/i }),
    ).not.toBeInTheDocument();
    // Cost is visible per assignment (all local → Free).
    expect(screen.getAllByText("Free").length).toBeGreaterThan(0);
  });

  it("changes a role assignment (the picker is enabled and the row reflects it)", async () => {
    // Assignments are now wired end-to-end (ConfigStore -> agent-server routing),
    // so the picker is operable, not a flagged dead control.
    const user = userEvent.setup();
    withQuery(<SettingsView />);
    await user.click(
      await screen.findByText(/Specialist role overrides/i, {
        selector: "summary",
      }),
    );
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

  it("reports unverified vision honestly, points to configuration, and can assign one", async () => {
    const user = userEvent.setup();
    withQuery(<SettingsView />);
    expect(
      await screen.findByText(/Vision hasn't been verified for Driver Local/i),
    ).toBeInTheDocument();
    const configureVision = screen.getByRole("link", {
      name: /Model library.*Image understanding/i,
    });
    expect(configureVision).toHaveAttribute("href", "#model-library");
    await user.click(configureVision);
    expect(document.querySelector("#model-library details")).toHaveAttribute("open");

    const trigger = screen.getByRole("button", {
      name: /Choose optional visual inspection model/i,
    });
    expect(trigger).toHaveTextContent(/Use main model when capable/i);
    await user.click(trigger);
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByText(/Driver Overflow/i));
    await waitFor(() => expect(trigger).toHaveTextContent(/Driver Overflow/i));
    expect(
      screen.queryByText(/Vision hasn't been verified/i),
    ).not.toBeInTheDocument();

    await user.click(trigger);
    await user.click(
      within(screen.getByRole("dialog")).getByText(/Use main model when capable/i),
    );
    await waitFor(() =>
      expect(trigger).toHaveTextContent(/Use main model when capable/i),
    );
  });

  it("recommends a visual model for a text-only primary and can assign one", async () => {
    const user = userEvent.setup();
    withQuery(<SettingsView />);

    const library = await screen.findByText("Model library", {
      selector: "summary *",
    });
    await user.click(library);
    await user.click(
      await screen.findByRole("button", { name: /Edit driver-local/i }),
    );

    const editDialog = await screen.findByRole("dialog");
    await user.click(
      within(editDialog).getByText(/Advanced model metadata/i, {
        selector: "summary",
      }),
    );
    await user.selectOptions(
      within(editDialog).getByRole("combobox", {
        name: /Image understanding/i,
      }),
      "false",
    );
    await user.click(
      within(editDialog).getByRole("button", { name: /Save changes/i }),
    );

    expect(
      await screen.findByText(/Driver Local .* is marked text-only/i),
    ).toBeInTheDocument();
    const trigger = screen.getByRole("button", {
      name: /Choose optional visual inspection model/i,
    });
    await user.click(trigger);
    await user.click(
      within(screen.getByRole("dialog")).getByText(/Driver Overflow/i),
    );
    await waitFor(() => expect(trigger).toHaveTextContent(/Driver Overflow/i));
    expect(
      screen.queryByText(/Driver Local .* is marked text-only/i),
    ).not.toBeInTheDocument();
  });
});

describe("Settings — Encoders (bundled-local vs remote)", () => {
  it("shows the encoder mode as an honest, wired toggle (default bundled-local)", async () => {
    const user = userEvent.setup();
    withQuery(<SettingsView />);
    await screen.findByRole("heading", { name: /Encoders/i });
    expect(await screen.findByText(/Current: Bundled \(local\)/i)).toBeInTheDocument();
    const configure = screen.getByText(/Configure encoders/i, {
      selector: "summary",
    });
    expect(configure.closest("details")).not.toHaveAttribute("open");
    await user.click(configure);
    expect(configure.closest("details")).toHaveAttribute("open");
    const bundled = await screen.findByRole("button", { name: /Bundled \(local\)/i });
    const remote = screen.getByRole("button", { name: /Remote endpoints/i });
    // Default is bundled-local (pressed); remote is the alternative.
    await waitFor(() => expect(bundled).toHaveAttribute("aria-pressed", "true"));
    expect(remote).toHaveAttribute("aria-pressed", "false");
    // Flipping to remote is a real action (persists + reflects).
    await user.click(remote);
    await waitFor(() => expect(remote).toHaveAttribute("aria-pressed", "true"));
  });
});

describe("Settings — Audio overview", () => {
  it("uses the same current-state plus Configure pattern as other media settings", async () => {
    const user = userEvent.setup();
    withQuery(<SettingsView />);

    expect(
      await screen.findByText(/Current: Bundled \(in-process\)/i),
    ).toBeInTheDocument();
    const configure = screen.getByText(/Configure audio overview/i, {
      selector: "summary",
    });
    expect(configure.closest("details")).not.toHaveAttribute("open");
    await user.click(configure);
    expect(configure.closest("details")).toHaveAttribute("open");
    expect(
      screen.getByRole("button", { name: /Self-hosted endpoint/i }),
    ).toBeInTheDocument();
  });
});

describe("Settings — model catalogue (CRUD)", () => {
  it("adds a new model via the form and it appears in the catalogue", async () => {
    const user = userEvent.setup();
    withQuery(<SettingsView />);
    const library = await screen.findByText("Model library", { selector: "summary *" });
    await user.click(library);

    await user.click(screen.getByRole("button", { name: /Add model/i }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByPlaceholderText("my-llama"), "test-model");
    await user.type(within(dialog).getByPlaceholderText(/llama-3.3-70b/i), "test.gguf");
    await user.type(
      within(dialog).getByLabelText("Model credential"),
      "LEGACY_CUSTOM_KEY",
    );
    await user.click(within(dialog).getByRole("button", { name: /^Add model$/i }));

    // the new model shows in the catalogue list (label derived like the backend)
    const label = await screen.findByText(/Test Model — test/i);
    const row = label.closest("li");
    expect(row).not.toBeNull();
    await user.click(
      within(row!).getByRole("button", { name: /Edit test-model/i }),
    );
    expect(
      within(await screen.findByRole("dialog")).getByLabelText(
        "Model credential",
      ),
    ).toHaveValue("LEGACY_CUSTOM_KEY");
  });

  it("lets the user explicitly mark a model as image-capable", async () => {
    const user = userEvent.setup();
    withQuery(<SettingsView />);
    const library = await screen.findByText("Model library", { selector: "summary *" });
    await user.click(library);

    await user.click(screen.getByRole("button", { name: /Add model/i }));
    const dialog = await screen.findByRole("dialog");
    const advanced = within(dialog).getByText(/Advanced model metadata/i, {
      selector: "summary",
    });
    expect(advanced.closest("details")).not.toHaveAttribute("open");
    await user.click(advanced);
    expect(advanced.closest("details")).toHaveAttribute("open");
    await user.type(within(dialog).getByPlaceholderText("my-llama"), "visual-test");
    await user.type(within(dialog).getByPlaceholderText(/llama-3.3-70b/i), "visual.gguf");
    await user.selectOptions(
      within(dialog).getByRole("combobox", { name: /Image understanding/i }),
      "true",
    );
    await user.click(within(dialog).getByRole("button", { name: /^Add model$/i }));

    const label = await screen.findByText(/Visual Test — visual/i);
    const row = label.closest("li");
    expect(row).not.toBeNull();
    await user.click(within(row!).getByRole("button", { name: /Edit visual-test/i }));
    const editDialog = await screen.findByRole("dialog");
    await user.click(
      within(editDialog).getByText(/Advanced model metadata/i, {
        selector: "summary",
      }),
    );
    expect(
      within(editDialog).getByRole("combobox", { name: /Image understanding/i }),
    ).toHaveValue("true");
  });
});

describe("Settings — information architecture", () => {
  it("separates interface, models, research, runtime, and extensions", async () => {
    withQuery(<SettingsView />);
    const nav = screen.getByRole("navigation", { name: /Settings sections/i });
    for (const label of [
      "General",
      "Models",
      "Research & Media",
      "Runtime",
      "Extensions & Storage",
    ]) {
      expect(within(nav).getByRole("link", { name: label })).toBeInTheDocument();
    }

    for (const [legacyId, currentId] of [
      ["models-providers", "models"],
      ["intelligence", "research-media"],
      ["agent-sandbox", "runtime"],
      ["workspace", "extensions-storage"],
    ] as const) {
      expect(document.getElementById(currentId)).toContainElement(
        document.getElementById(legacyId),
      );
    }

    const general = document.getElementById("general");
    const runtime = document.getElementById("runtime");
    expect(general).not.toBeNull();
    expect(runtime).not.toBeNull();
    const chat = await within(general!).findByRole("heading", {
      name: "Agent chat",
    });
    await within(runtime!).findByRole("heading", { name: "Sandbox" });
    expect(runtime).not.toContainElement(chat);

    const resilience = screen.getByText(/Advanced model resilience/i, {
      selector: "summary",
    });
    expect(resilience.closest("details")).not.toHaveAttribute("open");
    await userEvent.click(resilience);
    expect(resilience.closest("details")).toHaveAttribute("open");
    expect(
      await screen.findByRole("heading", { name: "Model resilience" }),
    ).toBeInTheDocument();

    // Local sandbox: the dependent noVNC form is absent rather than occupying a
    // full standalone Settings section. A short explanation remains discoverable.
    expect(
      await within(runtime!).findByText(
        /Live browser settings appear here when gVisor is active/i,
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Live browser" }),
    ).not.toBeInTheDocument();
  });
});

describe("Settings — OpenRouter", () => {
  it("saves the key and adds an OpenRouter model to the catalogue", async () => {
    const user = userEvent.setup();
    withQuery(<SettingsView />);
    const openRouter = await screen.findByText("OpenRouter", { selector: "summary" });
    expect(openRouter.closest("details")).not.toHaveAttribute("open");
    await user.click(openRouter);
    expect(openRouter.closest("details")).toHaveAttribute("open");

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

  it("lists MCP connections with a live (wired) add affordance", async () => {
    const user = userEvent.setup();
    withQuery(<SettingsView />);
    expect(await screen.findByText("Filesystem")).toBeInTheDocument();
    expect(screen.getByText("Connected")).toBeInTheDocument();
    // Rung B (RP-05b): the NotWired/PendingBadge scaffold is gone; "Add
    // connection" is a live button that opens the real create form. (Full
    // CRUD/approval coverage lives in McpSection.live/approval.test.tsx.)
    const addBtn = screen.getByRole("button", { name: /Add connection/i });
    expect(addBtn).toBeEnabled();
    await user.click(addBtn);
    expect(
      await screen.findByRole("textbox", { name: /MCP server name/i }),
    ).toBeInTheDocument();
  });
});

describe("Settings — sandbox", () => {
  it("offers the two live backends with isolation tiers legible; Podman is hidden for now", async () => {
    withQuery(<SettingsView />);
    expect(await screen.findByRole("heading", { name: "Sandbox" })).toBeInTheDocument();
    // radios render after the async config load
    expect(await screen.findByRole("radio", { name: /gVisor sandbox backend/i })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Local container sandbox backend/i })).toBeInTheDocument();
    // Podman is temporarily commented out of the picker (no live host in this environment
    // — see BACKEND_META in types/sandbox.ts). Guard its absence so a re-add without the
    // backing host work fails loudly here.
    expect(screen.queryByRole("radio", { name: /Podman .* sandbox backend/i })).toBeNull();
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
    expect(gvisor).toHaveAttribute("aria-checked", "false");
    await user.click(gvisor);
    const save = screen.getByRole("button", { name: /save sandbox/i });
    expect(save).toBeEnabled();
    await user.click(save);
    await waitFor(() => expect(screen.getByRole("button", { name: /^saved$/i })).toBeInTheDocument());
    const liveBrowser = await screen.findByRole("heading", {
      name: "Live browser",
    });
    expect(document.getElementById("live-browser")).toContainElement(liveBrowser);
    expect(
      await screen.findByRole("button", { name: /^On/i }),
    ).toBeInTheDocument();
  });
});
