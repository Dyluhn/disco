import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { WorkspaceVersion } from "@/api/agent";
import { VersionBanner } from "./VersionBanner";

const version = { seq: 2, tree_digest: "digest-2" } as WorkspaceVersion;
it("offers rollback only for a selected snapshot still in the server's version list", () => {
  const props = { selectedVersionSeq: 2, displayedVersionSeq: 2, restoringSeq: null,
    onBackToCurrent: vi.fn(), onRollback: vi.fn() };
  const view = render(<VersionBanner {...props} versions={[version]} />);
  expect(screen.getByRole("button", { name: /^roll back$/i })).toBeEnabled();
  view.rerender(<VersionBanner {...props} versions={[]} />);
  expect(screen.queryByRole("button", { name: /^roll back$/i })).not.toBeInTheDocument();
  view.rerender(<VersionBanner {...props} selectedVersionSeq={null} versions={[version]} />);
  expect(screen.queryByRole("button", { name: /^roll back$/i })).not.toBeInTheDocument();
  view.rerender(<VersionBanner {...props} selectedVersionSeq={9} versions={[version]} />);
  expect(screen.queryByRole("button", { name: /^roll back$/i })).not.toBeInTheDocument();
  expect(props.onRollback).not.toHaveBeenCalled();
});
