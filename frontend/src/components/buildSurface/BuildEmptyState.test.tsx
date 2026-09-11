/**
 * BuildEmptyState — regression guard for the Assist tier toggle staying HIDDEN.
 *
 * The control is deprecated-but-not-removed (see BuildEmptyState.tsx's header
 * comment and useBuild.ts's assistChoice/setAssistChoice): the state, the
 * reducer handling, and the API field it drives are all untouched and still
 * send the default (off). Only the switch that let a user flip it is gone
 * from render output.
 *
 * BuildEmptyState is the ONE component behind both the Build and the Agent
 * surface (`_BUILD_LIKE_SURFACES = {"build", "agent"}` on the backend; here
 * it's just the `framing` prop) — this test proves absence under both, rather
 * than assuming the Build case generalizes.
 *
 * Every other child is stubbed so this stays a hermetic, deterministic test of
 * BuildEmptyState's own render output — none of BuildModelPicker/
 * ConnectionsStrip/QueryInput's react-query or router dependencies are
 * exercised here (that's covered elsewhere).
 */

import { fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { BuildEmptyState } from "./BuildEmptyState";
import type { BuildController, FramingCopy } from "./types";

vi.mock("@/components/states", () => ({
  EmptyState: () => <div data-testid="stub-hero" />,
}));
// The stub renders the in-card slots (extraControls + footer) so the options
// toggle, the relocated config controls, and the driver notice — all of which
// live INSIDE the composer card now — stay assertable here.
vi.mock("@/components/QueryInput", () => ({
  QueryInput: ({ extraControls, footer }: { extraControls?: ReactNode; footer?: ReactNode }) => (
    <div data-testid="stub-query-input">
      {extraControls}
      {footer}
    </div>
  ),
}));
vi.mock("@/hooks/useDriverModels", () => ({
  useDriverModels: () => ({
    data: { models: [{ id: "drv-1", label: "Test Driver" }], default: "drv-1" },
  }),
  useLastSelectedModel: () => ({ data: null }),
}));
vi.mock("@/components/buildSurface/ReferencePackPicker", () => ({
  ReferencePackPicker: () => <div data-testid="stub-reference-packs" />,
}));
vi.mock("@/components/build/BuildModelPicker", () => ({
  BuildModelPicker: () => <div data-testid="stub-model-picker" />,
}));
// BuildEmptyState imports UploadComposer from the OTHER BuildSurface.tsx
// (components/build/), not this directory's decomposition — stub just that
// named export.
vi.mock("@/components/build/BuildSurface", () => ({
  UploadComposer: () => null,
}));
vi.mock("@/components/build/ImportProjectDialog", () => ({
  ImportProjectDialog: () => null,
}));
vi.mock("@/components/build/ConnectionsStrip", () => ({
  ConnectionsStrip: () => <div data-testid="stub-connections-strip" />,
}));
vi.mock("@/components/SuggestionChips", () => ({
  SuggestionChips: () => null,
}));

const copy: FramingCopy = {
  placeholder: "placeholder",
  replanPlaceholder: "replan placeholder",
  startError: "start error",
  heroTitle: "Build",
  heroSubtitle: "subtitle",
};

function makeController(): BuildController {
  return {
    modelId: null,
    setModelId: vi.fn(),
    // Mirrors useBuild's default: autonomous ON for new conversations.
    autonomousChoice: true,
    setAutonomousChoice: vi.fn(),
    // The deprecated tier choice — still present on the controller, still
    // flowing into submit()'s payload elsewhere (useBuild.ts); this test only
    // asserts nothing RENDERS it.
    assistChoice: false,
    setAssistChoice: vi.fn(),
    submit: vi.fn(),
    submitting: false,
    submitError: null,
    preCid: null,
    ensurePreCid: undefined,
  } as unknown as BuildController;
}

describe("BuildEmptyState — Assist tier toggle stays hidden", () => {
  it.each(["build", "agent"] as const)(
    "renders the Autonomous toggle but no Assist toggle (%s framing)",
    (framing) => {
      render(
        <BuildEmptyState
          framing={framing}
          copy={copy}
          b={makeController()}
          draft=""
          setDraft={() => {}}
        />,
      );

      // The relocated config controls live behind the options disclosure now.
      fireEvent.click(screen.getByRole("button", { name: /^Options/ }));

      // The deprecated control is gone from render output, not just visually
      // hidden — no role, no label, no data hook, no leftover on/off text.
      expect(screen.queryByRole("switch", { name: /assist/i })).not.toBeInTheDocument();
      expect(screen.queryByLabelText(/assist tier/i)).not.toBeInTheDocument();
      expect(screen.queryByText(/assist: on/i)).not.toBeInTheDocument();
      expect(screen.queryByText(/assist: off/i)).not.toBeInTheDocument();
      expect(
        document.querySelector('[data-disco-control="build.assist-toggle"]'),
      ).toBeNull();

      // Its sibling (deliberately kept this pass) proves the removal was
      // targeted at Assist specifically, not a wholesale gutting of the row.
      const autonomous = screen.getByRole("switch", { name: /autonomous mode/i });
      expect(autonomous).toHaveAttribute("data-disco-control", "build.autonomous-toggle");
    },
  );
});

describe("BuildEmptyState — composer-anchored config (no controls above the box)", () => {
  it.each(["build", "agent"] as const)(
    "keeps the model picker and Autonomous toggle inside the options panel (%s framing)",
    (framing) => {
      const b = makeController();
      render(
        <BuildEmptyState framing={framing} copy={copy} b={b} draft="" setDraft={() => {}} />,
      );

      // Nothing optional renders outside the menu: no config controls above
      // the box, no connections strip below it, and the old sandbox
      // boilerplate is gone for good (the plan gate says it when it matters).
      expect(screen.queryByTestId("stub-model-picker")).not.toBeInTheDocument();
      expect(screen.queryByRole("switch", { name: /autonomous mode/i })).not.toBeInTheDocument();
      expect(screen.queryByText(/works in a sandbox/i)).not.toBeInTheDocument();
      expect(screen.queryByTestId("stub-connections-strip")).not.toBeInTheDocument();

      // …and everything reappears, functional, inside the expandable options area.
      fireEvent.click(screen.getByRole("button", { name: /^Options/ }));
      expect(screen.getByTestId("stub-model-picker")).toBeInTheDocument();
      expect(screen.queryByText(/works in a sandbox/i)).not.toBeInTheDocument();
      // ConnectionsStrip is the Agent framing's tool signal — menu-only, and
      // only there.
      if (framing === "agent") {
        expect(screen.getByTestId("stub-connections-strip")).toBeInTheDocument();
      } else {
        expect(screen.queryByTestId("stub-connections-strip")).not.toBeInTheDocument();
      }
      const autonomous = screen.getByRole("switch", { name: /autonomous mode/i });
      expect(autonomous).toHaveAttribute("aria-checked", "true");
      fireEvent.click(autonomous);
      expect(b.setAutonomousChoice).toHaveBeenCalledWith(false);
    },
  );

  it("shows the driver-model notice in the card and expands the options panel on click", () => {
    render(
      <BuildEmptyState
        framing="build"
        copy={copy}
        b={makeController()}
        draft=""
        setDraft={() => {}}
      />,
    );

    const notice = screen.getByRole("button", { name: /driver model: Test Driver/i });
    expect(notice).toHaveAttribute("data-disco-control", "build.driver-model");
    expect(notice).toHaveTextContent("Test Driver");
    expect(screen.queryByTestId("stub-model-picker")).not.toBeInTheDocument();

    fireEvent.click(notice);
    expect(screen.getByTestId("stub-model-picker")).toBeInTheDocument();
  });
});
