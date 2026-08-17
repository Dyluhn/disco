import type {
  WorkflowAuthorInput,
  WorkflowDraftFromDescriptionResult,
  WorkflowDraftParam,
  WorkflowMcpMountInput,
  WorkflowReview,
} from "@/types/workflow";
import { DEFAULT_OUTPUT_FORMAT, DEFAULT_PARAM_TYPE, OUTPUT_FORMATS, PARAM_TYPES } from "./constants";
import type { OutputFormat, ParamType } from "./constants";

export function toggleValue(values: string[], value: string, checked: boolean): string[] {
  if (checked) return values.includes(value) ? values : [...values, value];
  return values.filter((item) => item !== value);
}

export function compactTags(value: string): string[] {
  return value
    .split(/[,\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

export function asRecord(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return value as Record<string, unknown>;
}

export function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item).trim()).filter(Boolean);
}

export function asOutputFormat(value: unknown): OutputFormat {
  return OUTPUT_FORMATS.includes(value as OutputFormat)
    ? (value as OutputFormat)
    : DEFAULT_OUTPUT_FORMAT;
}

export function paramTypeFromSchema(value: unknown): ParamType {
  const schema = asRecord(value);
  const type = schema.type;
  if (typeof type === "string" && PARAM_TYPES.includes(type as ParamType)) {
    return type as ParamType;
  }
  if (type === "array") {
    const itemType = asRecord(schema.items).type;
    if (itemType === "integer") return "integer_array";
    if (itemType === "number") return "number_array";
    return "string_array";
  }
  return DEFAULT_PARAM_TYPE;
}

export function paramsFromSchema(value: unknown): WorkflowDraftParam[] {
  const schema = asRecord(value);
  const properties = asRecord(schema.properties);
  const required = new Set(asStringArray(schema.required));
  return Object.entries(properties).map(([name, prop]) => {
    const propRecord = asRecord(prop);
    return {
      name,
      type: paramTypeFromSchema(prop),
      required: required.has(name),
      description:
        typeof propRecord.description === "string" ? propRecord.description : "",
    };
  });
}

export function paramsFromFriendly(value: unknown): WorkflowDraftParam[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => {
      const param = asRecord(item);
      const type = PARAM_TYPES.includes(param.type as ParamType)
        ? (param.type as ParamType)
        : DEFAULT_PARAM_TYPE;
      return {
        name: typeof param.name === "string" ? param.name : "",
        type,
        required: typeof param.required === "boolean" ? param.required : true,
        description:
          typeof param.description === "string" ? param.description : "",
      };
    })
    .filter((param) => param.name);
}

export function mcpMountsFromDefinition(value: unknown): WorkflowMcpMountInput[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => {
      const mount = asRecord(item);
      return {
        server: typeof mount.server === "string" ? mount.server : "",
        tool_names: asStringArray(mount.tool_names),
        read_only: typeof mount.read_only === "boolean" ? mount.read_only : true,
      };
    })
    .filter((mount) => mount.server);
}

export function authorInputFromReview(review: WorkflowReview): WorkflowAuthorInput {
  const definition = asRecord(review.definition);
  const policies = asRecord(definition.policies);
  const output = asRecord(definition.output_contract);
  const verify = asRecord(definition.verify);
  const schemaParams = paramsFromSchema(definition.params_model_schema);
  const params = schemaParams.length > 0 ? schemaParams : paramsFromFriendly(definition.params);
  return {
    name: typeof definition.name === "string" ? definition.name : review.name,
    card: typeof definition.card === "string" ? definition.card : review.card,
    params:
      params.length > 0
        ? params
        : [{ name: "query", type: DEFAULT_PARAM_TYPE, required: true, description: "" }],
    tools: asStringArray(definition.tools),
    mcp_mounts: mcpMountsFromDefinition(definition.mcp_mounts),
    skills: asStringArray(definition.skills),
    allows_writes:
      typeof policies.allows_writes === "boolean"
        ? policies.allows_writes
        : typeof definition.allows_writes === "boolean"
          ? definition.allows_writes
        : false,
    untrusted_content:
      typeof policies.untrusted_content === "boolean"
        ? policies.untrusted_content
        : typeof definition.untrusted_content === "boolean"
          ? definition.untrusted_content
        : true,
    output_path_template:
      typeof output.path_template === "string"
        ? output.path_template
        : typeof definition.output_path_template === "string"
          ? definition.output_path_template
        : "outputs/{query}.md",
    output_format: asOutputFormat(output.format ?? definition.output_format),
    verify_checks:
      asStringArray(verify.checks).length > 0
        ? asStringArray(verify.checks)
        : asStringArray(definition.verify_checks),
    finalizer:
      typeof verify.finalizer === "string"
        ? verify.finalizer
        : typeof definition.finalizer === "string"
          ? definition.finalizer
          : null,
  };
}

export function resultFromAuthorDraft(
  response: { workflow: WorkflowReview; simulation: WorkflowDraftFromDescriptionResult["simulation"] },
  description: string,
): WorkflowDraftFromDescriptionResult {
  return {
    ...response,
    summary: `Drafted workflow for review: ${response.workflow.name}.`,
    description,
  };
}

export function workflowUses(review: WorkflowReview): string[] {
  const definition = asRecord(review.definition);
  const uses = new Set<string>();
  for (const tool of asStringArray(definition.tools)) uses.add(tool);
  for (const mount of mcpMountsFromDefinition(definition.mcp_mounts)) {
    for (const tool of mount.tool_names) uses.add(`mcp:${mount.server}/${tool}`);
  }
  for (const skill of asStringArray(definition.skills)) uses.add(`skill:${skill}`);
  return Array.from(uses);
}

export function outputLabel(result: WorkflowDraftFromDescriptionResult): string {
  const definition = asRecord(result.workflow.definition);
  const output = asRecord(definition.output_contract);
  if (result.simulation.output_path) return result.simulation.output_path;
  if (typeof output.path_template === "string") return output.path_template;
  if (typeof definition.output_path_template === "string") return definition.output_path_template;
  return "No output path";
}

export function errorMessage(error: unknown, fallback: string): string {
  if (!(error instanceof Error)) return fallback;
  try {
    const parsed = JSON.parse(error.message) as unknown;
    const body = asRecord(parsed);
    const detail = asRecord(body.detail);
    if (typeof body.detail === "string") return body.detail;
    if (typeof detail.detail === "string") return detail.detail;
    if (typeof body.reason === "string") return body.reason;
  } catch {
    return error.message || fallback;
  }
  return error.message || fallback;
}
