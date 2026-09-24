import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { AgentEvent } from "@/types/agent";
import { FilesPane } from "./FilesPane";

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  agentLive: () => true,
}));
afterEach(() => vi.unstubAllGlobals());

it("reads current bytes after restore and excludes files absent from the restored manifest", async () => {
  let content = "Version two";
  const fetcher = vi.fn<typeof fetch>(async () => new Response(JSON.stringify({
    path: "index.html", content, binary: false, bytes: content.length, version_seq: 1,
  }), { headers: { "content-type": "application/json" } }));
  vi.stubGlobal("fetch", fetcher);
  const events = [{kind:"action", tool_call: {
    tool_name:"file_write", arguments:{path:"index.html",content:"stale event bytes"},
  }}, {kind:"action",tool_call:{
    tool_name:"file_write",arguments:{path:"deleted.txt",content:"removed"},
  }}] as unknown as AgentEvent[];
  const client = new QueryClient({defaultOptions:{queries:{retry:false}}});
  const view = (revision: string) => <QueryClientProvider client={client}>
    <FilesPane events={events} streamingFile={null}
      manifestFiles={[{path:"index.html",bytes:11}]}
      snapshot={{cid:"conv_example",revision,loading:false,error:false}} />
  </QueryClientProvider>;
  const {rerender} = render(view("version-2"));
  expect(await screen.findByText("Version two")).toBeInTheDocument();
  content = "Restored version one";
  rerender(view("restore-1"));
  expect(await screen.findByText("Restored version one")).toBeInTheDocument();
  expect(screen.queryByText("Version two")).not.toBeInTheDocument();
  expect(screen.queryByText("stale event bytes")).not.toBeInTheDocument();
  expect(screen.queryByText("deleted.txt")).not.toBeInTheDocument();
  expect(fetcher).toHaveBeenCalledTimes(2);
  expect(String(fetcher.mock.calls[0]?.[0])).toContain("/api/projects/conv_example/files/index.html");
});

it("shows a failed authoritative read without substituting historical content", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response("not found",{status:404})));
  const client = new QueryClient({defaultOptions:{queries:{retry:false}}});
  render(<QueryClientProvider client={client}>
    <FilesPane events={[]} streamingFile={null}
      manifestFiles={[{path:"missing.txt",bytes:11}]}
      snapshot={{cid:"conv_example",revision:"restore-1",loading:false,error:false}} />
  </QueryClientProvider>);
  expect(await screen.findByText(/Could not load the current file/)).toBeInTheDocument();
  expect(screen.queryByText("(empty)")).not.toBeInTheDocument();
});
