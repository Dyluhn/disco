export interface WorkflowParamSchema {
  properties?: Record<string, { type?: string; description?: string }>;
  required?: string[];
}

export interface ParsedWorkflowParams {
  params: Record<string, unknown>;
  errors: Record<string, string>;
}

type ParsedValue =
  | { ok: true; value: unknown }
  | { ok: false; error: string };

function parseJsonContainer(raw: string, kind: "array" | "object"): ParsedValue {
  try {
    const value: unknown = JSON.parse(raw);
    const valid =
      kind === "array"
        ? Array.isArray(value)
        : value !== null && typeof value === "object" && !Array.isArray(value);
    if (!valid) throw new Error(`not an ${kind}`);
    return { ok: true, value };
  } catch {
    return kind === "array"
      ? { ok: false, error: 'Enter a JSON array, for example ["value"]' }
      : { ok: false, error: 'Enter a JSON object, for example {"key":"value"}' };
  }
}

function parseWorkflowValue(type: string | undefined, raw: string): ParsedValue {
  if (type?.endsWith("array")) return parseJsonContainer(raw, "array");
  if (type === "object") return parseJsonContainer(raw, "object");
  if (type === "integer") {
    const value = Number(raw);
    return Number.isInteger(value)
      ? { ok: true, value }
      : { ok: false, error: "Enter a whole number" };
  }
  if (type === "number") {
    const value = Number(raw);
    return Number.isFinite(value)
      ? { ok: true, value }
      : { ok: false, error: "Enter a number" };
  }
  if (type === "boolean") {
    return raw === "true" || raw === "false"
      ? { ok: true, value: raw === "true" }
      : { ok: false, error: "Enter true or false" };
  }
  if (type === "null") {
    return raw === "null"
      ? { ok: true, value: null }
      : { ok: false, error: "Enter null" };
  }
  return { ok: true, value: raw };
}

/** Parse the small workflow input schema used by both Run and Scheduling. */
export function parseWorkflowParams(
  schema: WorkflowParamSchema,
  values: Record<string, string>,
): ParsedWorkflowParams {
  const errors: Record<string, string> = {};
  const params: Record<string, unknown> = {};
  for (const [name, definition] of Object.entries(schema.properties ?? {})) {
    const raw = (values[name] ?? "").trim();
    if (!raw && (schema.required ?? []).includes(name)) {
      errors[name] = "Required";
      continue;
    }
    if (!raw) continue;
    const parsed = parseWorkflowValue(definition.type, raw);
    if (parsed.ok) params[name] = parsed.value;
    else errors[name] = parsed.error;
  }
  return { params, errors };
}

export function initialWorkflowParams(
  schema: WorkflowParamSchema,
  existing: Record<string, unknown> = {},
): Record<string, string> {
  return Object.fromEntries(
    Object.keys(schema.properties ?? {}).map((name) => {
      const value = existing[name];
      return [
        name,
        typeof value === "string" ? value : value === undefined ? "" : JSON.stringify(value),
      ];
    }),
  );
}
