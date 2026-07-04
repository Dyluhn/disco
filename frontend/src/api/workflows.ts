import { ApiError, agentGet, agentLive, agentSend, fixtureDelay } from "./client";
import type { WorkflowListResponse, WorkflowReview } from "@/types/workflow";

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

const validSurface = {
  compiled: true,
  compile_error: null,
  allowed_tools: ["file_read", "finish", "needs_input", "skip"],
  advertised_tools: ["file_read", "finish", "needs_input", "skip"],
  tool_definitions: [
    {
      name: "file_read",
      description: "Read a file from the workspace.",
      parameters_schema: {
        type: "object",
        properties: { path: { type: "string" } },
        required: ["path"],
      },
      read_only: true,
      runs_in: "sandbox",
      base_risk: "LOW",
      needs: ["filesystem"],
      source: "builtin",
    },
    {
      name: "finish",
      description: "Declare the workflow complete.",
      parameters_schema: { type: "object", properties: { summary: { type: "string" } } },
      read_only: true,
      runs_in: "in_process",
      base_risk: "LOW",
      needs: [],
      source: "workflow_control",
    },
    {
      name: "needs_input",
      description: "Pause the workflow for required input.",
      parameters_schema: { type: "object", properties: { reason: { type: "string" } } },
      read_only: true,
      runs_in: "in_process",
      base_risk: "LOW",
      needs: [],
      source: "workflow_control",
    },
    {
      name: "skip",
      description: "Skip this workflow run.",
      parameters_schema: { type: "object", properties: { reason: { type: "string" } } },
      read_only: true,
      runs_in: "in_process",
      base_risk: "LOW",
      needs: [],
      source: "workflow_control",
    },
  ],
  mcp_mounts: [],
  skills: [],
  policies: { untrusted_content: true, allows_writes: false },
  params_model_schema: {
    type: "object",
    additionalProperties: false,
    properties: { query: { type: "string" } },
    required: ["query"],
  },
  output_contract: { path_template: "outputs/{query}.md", format: "markdown" },
  verify: { checks: ["output_exists"], finalizer: "ready_for_workflow_output" },
};

const fixtureWorkflows: WorkflowReview[] = [
  {
    instance_id: "wf_fixture_valid",
    name: "Fixture Valid Workflow",
    card: "Valid workflow review fixture with a bounded file-read surface.",
    definition_digest: "sha256:fixture-valid-definition",
    enabled: false,
    approved: false,
    approval: null,
    params: { query: "fixture" },
    definition: { name: "Fixture Valid Workflow" },
    validation_findings: [],
    compiled_surface: validSurface,
    surface_shown_digest: "sha256:fixture-valid-surface",
  },
  {
    instance_id: "wf_fixture_blocked",
    name: "Fixture Blocked Workflow",
    card: "Draft workflow with validation findings that block approval.",
    definition_digest: "sha256:fixture-blocked-definition",
    enabled: false,
    approved: false,
    approval: null,
    params: { query: "fixture" },
    definition: { name: "Fixture Blocked Workflow" },
    validation_findings: [
      {
        severity: "error",
        code: "unknown_builtin_tool",
        path: "tools",
        message: "unknown workflow builtin tool: missing_tool",
      },
    ],
    compiled_surface: {
      ...validSurface,
      compiled: false,
      compile_error: "unknown workflow builtin tool(s): missing_tool",
      allowed_tools: [],
      advertised_tools: [],
      tool_definitions: [],
    },
    surface_shown_digest: "sha256:fixture-blocked-surface",
  },
];

export async function listWorkflowReviews(): Promise<WorkflowListResponse> {
  if (agentLive()) return agentGet<WorkflowListResponse>("/api/workflows");
  await fixtureDelay();
  return { workflows: clone(fixtureWorkflows), status: "ok" };
}

export async function approveWorkflow(input: {
  instanceId: string;
  surfaceShownDigest: string;
}): Promise<WorkflowReview> {
  if (agentLive()) {
    const res = await agentSend<{ workflow: WorkflowReview }>(
      "POST",
      `/api/workflows/${encodeURIComponent(input.instanceId)}/approve`,
      {
        approved_by: "local",
        surface_shown_digest: input.surfaceShownDigest,
      },
    );
    return res.workflow;
  }

  await fixtureDelay();
  const workflow = fixtureWorkflows.find((wf) => wf.instance_id === input.instanceId);
  if (!workflow) throw new ApiError("workflow_not_found", 404);
  if (workflow.validation_findings.some((finding) => finding.severity === "error")) {
    throw new ApiError("workflow_validation_failed", 409);
  }
  workflow.enabled = true;
  workflow.approved = true;
  workflow.approval = {
    approved_at: new Date(0).toISOString(),
    approved_by: "local",
    surface_shown_digest: input.surfaceShownDigest,
  };
  return clone(workflow);
}
