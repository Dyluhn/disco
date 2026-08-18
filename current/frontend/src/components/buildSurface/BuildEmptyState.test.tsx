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

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { BuildEmptyState } from "./BuildEmptyState";
import type { BuildController, FramingCopy } from "./types";

vi.mock("@/components/states", () => ({
  EmptyState: () => <div data-testid="stub-hero" />,
}));
vi.mock("@/components/QueryInput", () => ({
  QueryInput: () => <div data-testid="stub-query-input" />,
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
    autonomousChoice: false,
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
