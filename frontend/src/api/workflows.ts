import { ApiError, agentGet, agentLive, agentSend, fixtureDelay } from "./client";
import type {
  WorkflowAuthorInput,
  WorkflowAuthoringContext,
  WorkflowDraftFromDescriptionResult,
  WorkflowDraftResult,
  WorkflowListResponse,
  WorkflowReview,
} from "@/types/workflow";

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

const fixtureAuthoringContext: WorkflowAuthoringContext = {
  builtin_tools: [
    {
      name: "file_read",
      description: "Read a file from the workspace.",
      read_only: true,
    },
    {
      name: "file_write",
      description: "Write a file in the workspace.",
      read_only: false,
    },
    {
      name: "browser",
      description: "Use a browser for bounded web inspection.",
      read_only: true,
    },
  ],
  mcp_servers: [
    {
      server: "github",
      tools: ["search_issues", "create_issue"],
    },
  ],
  skills: ["Release Notes", "Research Brief"],
  param_types: [
    "string",
    "integer",
    "number",
    "boolean",
    "string_array",
    "integer_array",
    "number_array",
  ],
  output_formats: ["markdown", "html", "json", "csv", "pptx", "pdf", "text"],
};

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

export async function getAuthoringContext(): Promise<WorkflowAuthoringContext> {
  if (agentLive()) return agentGet<WorkflowAuthoringContext>("/api/workflows/authoring-context");
  await fixtureDelay();
  return clone(fixtureAuthoringContext);
}

export async function authorWorkflow(input: WorkflowAuthorInput): Promise<WorkflowDraftResult> {
  if (agentLive()) {
    return agentSend<WorkflowDraftResult>("POST", "/api/workflows/author", input);
  }

  await fixtureDelay();
  const idSlug = input.name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_.-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60);
  const instanceId = `wf_${idSlug || "authored"}_${Date.now().toString(36)}`;
  const hasWritableMount = input.mcp_mounts.some((mount) => !mount.read_only);
  const findings =
    hasWritableMount && !input.allows_writes
      ? [
          {
            severity: "error" as const,
            code: "write_policy_inconsistent",
            path: "policies.allows_writes",
            message: "writable MCP mounts require policies.allows_writes=True",
          },
        ]
      : [];
  const review: WorkflowReview = {
    instance_id: instanceId,
    name: input.name,
    card: input.card,
    definition_digest: `sha256:${instanceId}-definition`,
    enabled: false,
    approved: false,
    approval: null,
    params: Object.fromEntries(input.params.map((param) => [param.name, "sample"])),
    definition: input,
    validation_findings: findings,
    compiled_surface: {
      ...validSurface,
      allowed_tools: [...input.tools, "finish", "needs_input", "skip"],
      advertised_tools: [...input.tools, "finish", "needs_input", "skip"],
      tool_definitions: validSurface.tool_definitions.filter((tool) =>
        [...input.tools, "finish", "needs_input", "skip"].includes(tool.name),
      ),
      mcp_mounts: input.mcp_mounts,
      skills: input.skills.map((name) => ({ requested: name, available: true })),
      policies: {
        untrusted_content: input.untrusted_content,
        allows_writes: input.allows_writes,
      },
      output_contract: {
        path_template: input.output_path_template,
        format: input.output_format,
      },
      verify: { checks: input.verify_checks, finalizer: input.finalizer },
    },
    surface_shown_digest: `sha256:${instanceId}-surface`,
  };
  fixtureWorkflows.unshift(review);
  return {
    workflow: clone(review),
    simulation: {
      ok: findings.length === 0,
      output_path: input.output_path_template.replace(/\{[^}]+\}/g, "sample"),
      output_format: input.output_format,
      fixture_bytes: 72,
      findings,
    },
  };
}

export async function draftWorkflowFromDescription(
  description: string,
): Promise<WorkflowDraftFromDescriptionResult> {
  if (agentLive()) {
    return agentSend<WorkflowDraftFromDescriptionResult>(
      "POST",
      "/api/workflows/draft-from-description",
      { description },
    );
  }

  const cleaned = description.trim();
  if (!cleaned) throw new ApiError("description is empty", 422);
  const needsWrites = /\b(save|write|create|export|update|append)\b/i.test(cleaned);
  const input: WorkflowAuthorInput = {
    name: "Drafted Workflow",
    card:
      "Workflow drafted from a plain-language description for review before approval.",
    params: [
      {
        name: "request",
        type: "string",
        required: true,
        description: "The request or topic to process.",
      },
    ],
    tools: needsWrites ? ["file_read", "file_write"] : ["file_read"],
    mcp_mounts: [],
    skills: [],
    allows_writes: needsWrites,
    untrusted_content: true,
    output_path_template: "outputs/{request}.md",
    output_format: "markdown",
    verify_checks: ["output_exists"],
    finalizer: "ready_for_workflow_output",
  };
  const drafted = await authorWorkflow(input);
  return {
    ...drafted,
    summary: `Drafts a workflow from this request: ${cleaned}`,
    description: cleaned,
  };
}

export async function runWorkflow(
  instanceId: string,
): Promise<{ conversation_id: string; status: string }> {
  if (agentLive()) {
    return agentSend<{ conversation_id: string; status: string }>(
      "POST",
      `/api/workflows/${encodeURIComponent(instanceId)}/run`,
    );
  }
  await fixtureDelay();
  return { conversation_id: `conv_${instanceId.slice(0, 16)}`, status: "started" };
}

export async function scheduleWorkflow(input: {
  instanceId: string;
  instanceDigest: string;
  cron: string;
}): Promise<Record<string, unknown>> {
  if (agentLive()) {
    return agentSend<Record<string, unknown>>("POST", "/api/workflows/schedules", {
      instance_id: input.instanceId,
      instance_digest: input.instanceDigest,
      cron: input.cron,
      enabled: true,
    });
  }
  await fixtureDelay();
  return {
    schedule_id: `wfsched_${input.instanceId}`,
    spec: {
      instance_id: input.instanceId,
      instance_digest: input.instanceDigest,
      cron: input.cron,
      enabled: true,
    },
  };
}
