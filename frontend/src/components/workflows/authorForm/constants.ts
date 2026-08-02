import type { WorkflowAuthoringContext } from "@/types/workflow";

export type ParamType = WorkflowAuthoringContext["param_types"][number];
export type OutputFormat = WorkflowAuthoringContext["output_formats"][number];

export const DEFAULT_PARAM_TYPE: ParamType = "string";
export const DEFAULT_OUTPUT_FORMAT: OutputFormat = "markdown";

export const PARAM_TYPES: readonly ParamType[] = [
  "string",
  "integer",
  "number",
  "boolean",
  "string_array",
  "integer_array",
  "number_array",
];

export const OUTPUT_FORMATS: readonly OutputFormat[] = [
  "markdown",
  "html",
  "json",
  "csv",
  "pptx",
  "pdf",
  "text",
];
