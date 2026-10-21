import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { expect, it, vi } from "vitest";
import { ModeProvider } from "@/shell/ModeProvider";
import { useMode } from "@/shell/mode";
import { ProjectsView } from "./ProjectsView";
vi.mock("@/hooks/useProjects", () => ({
  useProjects: () => ({ data: { projects: [], status: "ok" }, isLoading: false }),
  useDeleteProject: () => ({ mutate: vi.fn() }), useDownloadProject: vi.fn(), useProjectRelease: vi.fn(),
}));
function Probe() {
  const { mode, setMode } = useMode();
  return <><button onClick={() => setMode("agent")}>Select Agent</button><output>{mode}:{useLocation().pathname}</output></>;
}
it("Start a build selects Build even after using Agent", () => {
  render(<MemoryRouter initialEntries={["/projects"]}><ModeProvider><Probe /><ProjectsView /></ModeProvider></MemoryRouter>);
  fireEvent.click(screen.getByText("Select Agent"));
  expect(screen.getByRole("status")).toHaveTextContent("agent:/projects");
  fireEvent.click(screen.getByText("Start a build"));
  expect(screen.getByRole("status")).toHaveTextContent("build:/");
});
