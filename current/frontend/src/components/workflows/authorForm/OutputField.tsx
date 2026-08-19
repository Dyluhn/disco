import type { WorkflowAuthoringContext } from "@/types/workflow";
import type { OutputFormat } from "./constants";

interface OutputFieldProps {
  outputPathTemplate: string;
  onOutputPathTemplateChange: (value: string) => void;
  outputFormat: OutputFormat;
  onOutputFormatChange: (value: OutputFormat) => void;
  outputFormats: WorkflowAuthoringContext["output_formats"];
}

export function OutputField({
  outputPathTemplate,
  onOutputPathTemplateChange,
  outputFormat,
  onOutputFormatChange,
  outputFormats,
}: OutputFieldProps) {
  return (
    <section className="grid gap-inline md:grid-cols-[1fr_12rem]">
      <label className="flex flex-col gap-hair font-ui text-[0.8rem] text-text-muted">
        Output path template
        <input
          value={outputPathTemplate}
          onChange={(event) => onOutputPathTemplateChange(event.target.value)}
          className="min-h-11 rounded-control border border-hairline bg-surface-2 px-inline py-hair text-[0.84rem] text-text outline-none focus:border-accent lg:min-h-0"
        />
        <span className="text-text-faint">Example: outputs/{"{param}"}.md</span>
      </label>
      <label className="flex flex-col gap-hair font-ui text-[0.8rem] text-text-muted">
        Format
        <select
          aria-label="Output format"
          value={outputFormat}
          onChange={(event) => onOutputFormatChange(event.target.value as OutputFormat)}
          className="min-h-11 rounded-control border border-hairline bg-surface-2 px-inline py-hair text-[0.84rem] text-text outline-none focus:border-accent lg:min-h-0"
        >
          {outputFormats.map((format) => (
            <option key={format} value={format}>
              {format}
            </option>
          ))}
        </select>
      </label>
    </section>
  );
}
