export interface WorkflowParamSchema {
  properties?: Record<string, { type?: string; description?: string }>;
  required?: string[];
}

export interface ParsedWorkflowParams {
  params: Record<string, unknown>;
  errors: Record<string, string>;
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
    if (definition.type?.endsWith("array")) {
      try {
        const value: unknown = JSON.parse(raw);
        if (!Array.isArray(value)) throw new Error("not an array");
        params[name] = value;
      } catch {
        errors[name] = 'Enter a JSON array, for example ["value"]';
      }
    } else if (definition.type === "integer") {
      const value = Number(raw);
      if (!Number.isInteger(value)) errors[name] = "Enter a whole number";
      else params[name] = value;
    } else if (definition.type === "number") {
      const value = Number(raw);
      if (!Number.isFinite(value)) errors[name] = "Enter a number";
      else params[name] = value;
    } else if (definition.type === "boolean") {
      if (raw !== "true" && raw !== "false") errors[name] = "Enter true or false";
      else params[name] = raw === "true";
    } else if (definition.type === "object") {
      try {
        const value: unknown = JSON.parse(raw);
        if (value === null || typeof value !== "object" || Array.isArray(value)) {
          throw new Error("not an object");
        }
        params[name] = value;
      } catch {
        errors[name] = 'Enter a JSON object, for example {"key":"value"}';
      }
    } else if (definition.type === "null") {
      if (raw !== "null") errors[name] = "Enter null";
      else params[name] = null;
    } else {
      params[name] = raw;
    }
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
