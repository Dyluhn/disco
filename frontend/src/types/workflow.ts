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
