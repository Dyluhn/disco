import { useEffect, useMemo, useRef, useState } from "react";
import { FileCode2, PenLine } from "lucide-react";
import { cn } from "@/lib/cn";
import { deriveFiles } from "@/lib/buildTrace";
import type { StreamingFile } from "@/hooks/useBuildStream";
import type { AgentEvent } from "@/types/agent";
import { Empty } from "./Empty";

/** The live watch-it-write pane: the file the driver is composing/editing RIGHT NOW,
 *  content growing with a blinking cursor. Auto-scrolls to follow the tail so the
 *  newest line is always visible (the whole point — see it isn't hung). */
function StreamingFileView({ file }: { file: StreamingFile }) {
  const tailRef = useRef<HTMLDivElement | null>(null);
  const verb = file.tool === "file_edit" ? "editing" : "writing";
  useEffect(() => {
    // follow the writing edge; cheap because content only ever appends
    tailRef.current?.scrollIntoView({ block: "end" });
  }, [file.content]);
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center justify-between border-b border-accent/30 bg-accent/5 px-body py-hair font-mono text-[0.74rem]">
        <span className="flex items-center gap-hair text-accent">
          <PenLine className="size-3 shrink-0 animate-pulse" aria-hidden />
          <span className="truncate text-text">{file.path || `${verb}...`}</span>
        </span>
        <span className="text-text-faint">
          {file.content.length} B · {verb}
        </span>
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        <pre className="whitespace-pre-wrap px-body py-inline font-mono text-[0.78rem] leading-relaxed text-text">
          {file.content}
          <span className="ml-px inline-block animate-pulse text-accent">▋</span>
          <div ref={tailRef} />
        </pre>
      </div>
    </div>
  );
}

export function FilesPane({
  events,
  streamingFile,
}: {
  events: AgentEvent[];
  streamingFile: StreamingFile | null;
}) {
  const files = useMemo(() => deriveFiles(events), [events]);
  const [active, setActive] = useState(0);
  // While a file mutation is streaming, it is the hero — show the live buffer regardless
  // of which file was selected. The final ActionEvent retires it (streamingFile →
  // null) and the file then appears in the list as a normal, complete entry.
  if (streamingFile) return <StreamingFileView file={streamingFile} />;
  if (files.length === 0)
    return <Empty>No files written yet. Files the agent creates appear here.</Empty>;
  const file = files[Math.min(active, files.length - 1)];
  return (
    <div className="flex h-full min-h-0">
      <ul className="w-44 shrink-0 overflow-y-auto border-r border-hairline py-hair">
        {files.map((f, i) => (
          <li key={f.path}>
            <button
              type="button"
              onClick={() => setActive(i)}
              className={cn(
                "flex w-full items-center gap-hair truncate px-inline py-hair text-left font-mono text-[0.76rem] transition-colors",
                i === active ? "bg-surface-2 text-text" : "text-text-muted hover:text-text",
              )}
            >
              <FileCode2 className="size-3 shrink-0" aria-hidden />
              <span className="truncate">{f.path}</span>
            </button>
          </li>
        ))}
      </ul>
      <div className="min-w-0 flex-1 overflow-auto">
        <div className="flex items-center justify-between border-b border-hairline px-body py-hair font-mono text-[0.74rem] text-text-faint">
          <span>{file.path}</span>
          {/* C5: server-side artifacts land with bytes=0 — suppress the misleading "0 B" */}
          {file.bytes > 0 && <span>{file.bytes} B</span>}
        </div>
        <pre className="overflow-auto whitespace-pre-wrap px-body py-inline font-mono text-[0.78rem] leading-relaxed text-text">
          {file.content || "(empty)"}
        </pre>
      </div>
    </div>
  );
}
