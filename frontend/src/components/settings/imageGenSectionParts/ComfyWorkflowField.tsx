import { cn } from "@/lib/cn";
import { FIELD_CLASS } from "./styles";

export function ComfyWorkflowField({
  workflowJson,
  setWorkflowJson,
  workflowJsonError,
}: {
  workflowJson: string;
  setWorkflowJson: (value: string) => void;
  workflowJsonError: string | null;
}) {
  return (
    <label className="flex flex-col gap-hair">
      <span className="flex items-baseline gap-hair font-ui text-[0.8rem] text-text">
        Custom workflow
        <span className="font-ui text-[0.72rem] text-text-faint">
          · optional — empty = built-in SDXL graph
        </span>
      </span>
      <textarea
        spellCheck={false}
        rows={6}
        value={workflowJson}
        onChange={(e) => setWorkflowJson(e.target.value)}
        placeholder={
          '{ "1": { "class_type": "CheckpointLoaderSimple", "inputs": { "ckpt_name": "%ckpt%" } }, ... }\n' +
          "Paste a ComfyUI 'Save (API Format)' export. Tokens: %prompt% %negative% %seed% %width% %height% %ckpt%"
        }
        className={cn(FIELD_CLASS, "resize-y leading-snug")}
      />
      <span className="font-ui text-[0.72rem] text-text-faint">
        Overrides the default graph so FLUX / SD3 / custom shapes
        work. Tokens are substituted per generation: string tokens{" "}
        <code className="font-mono">%prompt%</code>{" "}
        <code className="font-mono">%negative%</code>{" "}
        <code className="font-mono">%ckpt%</code> go inside quotes;
        numeric tokens <code className="font-mono">%seed%</code>{" "}
        <code className="font-mono">%width%</code>{" "}
        <code className="font-mono">%height%</code> go UNQUOTED
        (e.g. <code className="font-mono">"seed": %seed%</code>).
      </span>
      {workflowJsonError && (
        <span className="font-ui text-[0.76rem] text-warn" role="status">
          ⚠ {workflowJsonError}
        </span>
      )}
    </label>
  );
}
