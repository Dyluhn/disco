import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { BuildDeliverableSection } from "./BuildDeliverableSection";
import type { BuildController } from "./types";
import type { CommittedFinish } from "@/lib/committedFinish";
import type { useProjectRelease } from "@/hooks/useProjects";
import type { ReleaseResponse } from "@/types/release";

vi.mock("@/components/toastApi", () => ({
  useToast: () => ({ show: vi.fn() }),
}));

const finish = {
  terminalSeq: 10,
  versionSeq: 3,
  treeDigest: "tree-3",
} as CommittedFinish;

function build(): BuildController {
  return { cid: "cid-finished", status: "FINISHED" } as unknown as BuildController;
}

function releaseState(overrides: Record<string, unknown> = {}) {
  return {
    data: undefined,
    error: null,
    isLoading: false,
    isFetching: false,
    refetch: vi.fn(),
    ...overrides,
  } as unknown as ReturnType<typeof useProjectRelease>;
}

function renderSection(release: ReturnType<typeof useProjectRelease>) {
  return render(
    <BuildDeliverableSection
      b={build()}
      deliverable={null}
      download={{ mutate: vi.fn(), isPending: false } as never}
      exportManifest={{ mutate: vi.fn(), isPending: false } as never}
      release={release}
      committedFinish={finish}
    />,
  );
}

function candidateRelease(): ReleaseResponse {
  return {
    assessment: "candidate",
    reasons: [],
    blockers: [],
    required_env: [],
    command: "docker compose up -d --build",
    ingress: null,
    self_host: true,
    spec_digest: "sha256:spec",
    version_seq: 3,
    tree_digest: "tree-3",
  };
}

describe("BuildDeliverableSection release handoff states", () => {
  it("shows a loading state after the committed seal while release is pending", () => {
    renderSection(releaseState({ isLoading: true, isFetching: true }));
    expect(screen.getByRole("status")).toHaveTextContent(/preparing self-host details/i);
    expect(document.querySelector('[data-disco-control="build.self-host-loading"]')).toHaveAttribute(
      "aria-busy",
      "true",
    );
  });

  it("shows release errors and retries only when the user asks", async () => {
    const user = userEvent.setup();
    const refetch = vi.fn();
    renderSection(
      releaseState({
        error: Object.assign(new Error("mismatch"), { name: "ReleaseSealMismatchError" }),
        refetch,
      }),
    );

    expect(screen.getByRole("alert")).toHaveTextContent(/no longer match/i);
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(refetch).toHaveBeenCalledOnce();
  });

  it("renders the self-host card when the sealed release matches", () => {
    renderSection(releaseState({ data: candidateRelease() }));
    expect(screen.getByRole("region", { name: /self-host this project/i })).toBeInTheDocument();
    expect(screen.queryByTestId("build.self-host-error")).not.toBeInTheDocument();
  });
});
