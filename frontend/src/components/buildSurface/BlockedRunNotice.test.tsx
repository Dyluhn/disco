import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import type { AgentEvent } from "@/types/agent";
import { BlockedRunNotice } from "./BlockedRunNotice";

const events = [
  {kind:"message",meta:{blocked_landing:true},message:{role:"assistant",content:"Provide the missing input file to continue."}},
  {kind:"status",status:"PAUSED",meta:{blocked_landing:true,blocked_reason:"Missing input file"}},
  {kind:"workspace_version",version_seq:1},
] as unknown as AgentEvent[];

it("shows the actionable blocked reply after reload and clears it on a later run", () => {
  const {rerender} = render(<BlockedRunNotice events={events} status="PAUSED" />);
  expect(screen.getByRole("status")).toHaveTextContent("Provide the missing input file to continue.");
  rerender(<BlockedRunNotice events={events} status="RUNNING" />);
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  rerender(<BlockedRunNotice events={[...events,{kind:"status",status:"PAUSED",meta:{}} as AgentEvent]} status="PAUSED" />);
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});
