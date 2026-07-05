export interface WorkflowValidationFinding {
  severity: "error" | "warning";
  code: string;
  path: string;
  message: string;
}

export interface WorkflowToolDefinition {
  name: string;
  description?: string;
  parameters_schema?: unknown;
  read_only?: boolean;
  runs_in?: "sandbox" | "in_process" | string;
  base_risk?: string | null;
  needs?: string[];
  source?: string;
  available?: boolean;
}

export interface WorkflowCompiledSurface {
  compiled: boolean;
  compile_error: string | null;
  allowed_tools: string[];
  advertised_tools: string[];
  tool_definitions: WorkflowToolDefinition[];
  mcp_mounts: unknown[];
  skills: unknown[];
  policies: Record<string, unknown>;
  params_model_schema: unknown;
  output_contract: unknown;
  verify: unknown;
}

export interface WorkflowApproval {
  approved_at: string;
  approved_by: string;
  surface_shown_digest: string;
}

export interface WorkflowReview {
  instance_id: string;
  name: string;
  card: string;
  definition_digest: string;
  enabled: boolean;
  approved: boolean;
  approval: WorkflowApproval | null;
  params: Record<string, unknown>;
  definition: unknown;
  validation_findings: WorkflowValidationFinding[];
  compiled_surface: WorkflowCompiledSurface;
  surface_shown_digest: string;
}

export interface WorkflowListResponse {
  workflows: WorkflowReview[];
  status: string;
}

export interface WorkflowAuthoringContext {
  builtin_tools: Array<{
    name: string;
    description: string;
    read_only: boolean;
  }>;
  mcp_servers: Array<{
    server: string;
    tools: string[];
  }>;
  skills: string[];
  param_types: Array<
    | "string"
    | "integer"
    | "number"
    | "boolean"
    | "string_array"
    | "integer_array"
    | "number_array"
  >;
  output_formats: Array<"markdown" | "html" | "json" | "csv" | "pptx" | "pdf" | "text">;
  status?: string;
}

export interface WorkflowDraftParam {
  name: string;
  type: WorkflowAuthoringContext["param_types"][number];
  required: boolean;
  description?: string;
}

export interface WorkflowMcpMountInput {
  server: string;
  tool_names: string[];
  read_only: boolean;
}

export interface WorkflowAuthorInput {
  name: string;
  card: string;
  params: WorkflowDraftParam[];
  tools: string[];
  mcp_mounts: WorkflowMcpMountInput[];
  skills: string[];
  allows_writes: boolean;
  untrusted_content: boolean;
  output_path_template: string;
  output_format: WorkflowAuthoringContext["output_formats"][number];
  verify_checks: string[];
  finalizer: string | null;
}

export interface WorkflowSimulation {
  ok: boolean;
  output_path: string | null;
  output_format: string | null;
  fixture_bytes: number;
  findings: WorkflowValidationFinding[];
}

export interface WorkflowDraftResult {
  workflow: WorkflowReview;
  simulation: WorkflowSimulation;
}

export interface WorkflowDraftFromDescriptionResult extends WorkflowDraftResult {
  summary: string;
  description: string;
}
