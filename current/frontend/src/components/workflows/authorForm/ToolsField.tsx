import type { WorkflowAuthoringContext } from "@/types/workflow";
import { toggleValue } from "./draftMapping";

interface ToolsFieldProps {
  builtinTools: WorkflowAuthoringContext["builtin_tools"];
  selectedTools: string[];
  onChange: (tools: string[]) => void;
}

export function ToolsField({ builtinTools, selectedTools, onChange }: ToolsFieldProps) {
  return (
    <section className="flex flex-col gap-inline">
      <h4 className="font-ui text-[0.86rem] font-semibold text-text">Tools</h4>
      <div className="grid gap-hair md:grid-cols-2">
        {builtinTools.map((tool) => (
          <label
            key={tool.name}
            className="flex items-start gap-hair rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.8rem] text-text"
          >
            <input
              type="checkbox"
              checked={selectedTools.includes(tool.name)}
              onChange={(event) =>
                onChange(toggleValue(selectedTools, tool.name, event.target.checked))
              }
              className="mt-[0.2rem]"
            />
            <span className="min-w-0">
              <span className="flex flex-wrap items-center gap-hair">
                <span className="font-semibold">{tool.name}</span>
                <span className="rounded-full border border-hairline px-hair text-[0.68rem] text-text-muted">
                  {tool.read_only ? "read-only" : "writes"}
                </span>
              </span>
              <span className="block text-text-muted">{tool.description}</span>
            </span>
          </label>
        ))}
      </div>
    </section>
  );
}
