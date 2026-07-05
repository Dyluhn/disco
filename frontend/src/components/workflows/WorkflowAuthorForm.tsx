import {
  AlertTriangle,
  ArrowRight,
  Check,
  CheckCircle2,
  Loader2,
  Pencil,
  Plus,
  RotateCcw,
  Trash2,
  X,
} from "lucide-react";
import { useMemo, useState } from "react";
import type { FormEvent, KeyboardEvent } from "react";
import {
  useApproveWorkflow,
  useAuthoringContext,
  useAuthorWorkflow,
  useDraftFromDescription,
} from "@/hooks/useWorkflows";
import type {
  WorkflowAuthorInput,
  WorkflowAuthoringContext,
  WorkflowDraftFromDescriptionResult,
  WorkflowDraftParam,
  WorkflowMcpMountInput,
  WorkflowReview,
  WorkflowValidationFinding,
} from "@/types/workflow";

type ParamType = WorkflowAuthoringContext["param_types"][number];
type OutputFormat = WorkflowAuthoringContext["output_formats"][number];

const DEFAULT_PARAM_TYPE: ParamType = "string";
const DEFAULT_OUTPUT_FORMAT: OutputFormat = "markdown";
const PARAM_TYPES: readonly ParamType[] = [
  "string",
  "integer",
  "number",
  "boolean",
  "string_array",
  "integer_array",
  "number_array",
];
const OUTPUT_FORMATS: readonly OutputFormat[] = [
  "markdown",
  "html",
  "json",
  "csv",
  "pptx",
  "pdf",
  "text",
];

function toggleValue(values: string[], value: string, checked: boolean): string[] {
  if (checked) return values.includes(value) ? values : [...values, value];
  return values.filter((item) => item !== value);
}

function compactTags(value: string): string[] {
  return value
    .split(/[,\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function asRecord(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return value as Record<string, unknown>;
}

function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item).trim()).filter(Boolean);
}

function asOutputFormat(value: unknown): OutputFormat {
  return OUTPUT_FORMATS.includes(value as OutputFormat)
    ? (value as OutputFormat)
    : DEFAULT_OUTPUT_FORMAT;
}

function paramTypeFromSchema(value: unknown): ParamType {
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

function paramsFromSchema(value: unknown): WorkflowDraftParam[] {
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

function paramsFromFriendly(value: unknown): WorkflowDraftParam[] {
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

function mcpMountsFromDefinition(value: unknown): WorkflowMcpMountInput[] {
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

function authorInputFromReview(review: WorkflowReview): WorkflowAuthorInput {
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

function resultFromAuthorDraft(
  response: { workflow: WorkflowReview; simulation: WorkflowDraftFromDescriptionResult["simulation"] },
  description: string,
): WorkflowDraftFromDescriptionResult {
  return {
    ...response,
    summary: `Drafted workflow for review: ${response.workflow.name}.`,
    description,
  };
}

function workflowUses(review: WorkflowReview): string[] {
  const definition = asRecord(review.definition);
  const uses = new Set<string>();
  for (const tool of asStringArray(definition.tools)) uses.add(tool);
  for (const mount of mcpMountsFromDefinition(definition.mcp_mounts)) {
    for (const tool of mount.tool_names) uses.add(`mcp:${mount.server}/${tool}`);
  }
  for (const skill of asStringArray(definition.skills)) uses.add(`skill:${skill}`);
  return Array.from(uses);
}

function outputLabel(result: WorkflowDraftFromDescriptionResult): string {
  const definition = asRecord(result.workflow.definition);
  const output = asRecord(definition.output_contract);
  if (result.simulation.output_path) return result.simulation.output_path;
  if (typeof output.path_template === "string") return output.path_template;
  if (typeof definition.output_path_template === "string") return definition.output_path_template;
  return "No output path";
}

function errorMessage(error: unknown, fallback: string): string {
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

function FindingRows({ findings }: { findings: WorkflowValidationFinding[] }) {
  if (findings.length === 0) {
    return (
      <div className="flex items-center gap-hair font-ui text-[0.8rem] text-text-muted">
        <CheckCircle2 className="size-4 text-supported" aria-hidden />
        No validation findings.
      </div>
    );
  }
  return (
    <ul className="flex flex-col gap-hair">
      {findings.map((finding, index) => (
        <li
          key={`${finding.code}-${finding.path}-${index}`}
          className="rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.8rem]"
          data-severity={finding.severity}
        >
          <span
            className={
              finding.severity === "error"
                ? "font-semibold text-unsupported"
                : "font-semibold text-warn"
            }
          >
            {finding.severity}
          </span>
          <span className="text-text-muted"> · {finding.code}</span>
          <div className="text-text">{finding.message}</div>
          <div className="font-mono text-[0.7rem] text-text-faint">{finding.path}</div>
        </li>
      ))}
    </ul>
  );
}

export function WorkflowAuthorForm() {
  const context = useAuthoringContext();
  const author = useAuthorWorkflow();
  const draft = useDraftFromDescription();
  const approve = useApproveWorkflow();
  const [description, setDescription] = useState("");
  const [result, setResult] = useState<WorkflowDraftFromDescriptionResult | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [name, setName] = useState("");
  const [card, setCard] = useState("");
  const [tools, setTools] = useState<string[]>([]);
  const [params, setParams] = useState<WorkflowDraftParam[]>([
    { name: "query", type: DEFAULT_PARAM_TYPE, required: true, description: "" },
  ]);
  const [mcpMounts, setMcpMounts] = useState<WorkflowMcpMountInput[]>([]);
  const [skills, setSkills] = useState<string[]>([]);
  const [outputPathTemplate, setOutputPathTemplate] = useState("outputs/{query}.md");
  const [outputFormat, setOutputFormat] = useState<OutputFormat>(DEFAULT_OUTPUT_FORMAT);
  const [verifyChecks, setVerifyChecks] = useState<string[]>([]);
  const [verifyDraft, setVerifyDraft] = useState("");
  const [finalizer, setFinalizer] = useState("");
  const [allowsWrites, setAllowsWrites] = useState(false);
  const [untrustedContent, setUntrustedContent] = useState(true);

  const authoring = context.data;
  const paramNames = params.map((param) => param.name.trim()).filter(Boolean);
  const duplicateParams = new Set(paramNames).size !== paramNames.length;
  const cardHasBlankLine = /\n\s*\n/.test(card);
  const writableMcpNeedsPolicy = mcpMounts.some((mount) => !mount.read_only) && !allowsWrites;
  const emptyMcpMount = mcpMounts.some((mount) => mount.tool_names.length === 0);
  const invalidParam = params.some((param) => !param.name.trim());
  const canDraft = Boolean(description.trim()) && !draft.isPending;
  const canSubmit =
    Boolean(authoring) &&
    Boolean(name.trim()) &&
    Boolean(card.trim()) &&
    Boolean(outputPathTemplate.trim()) &&
    !cardHasBlankLine &&
    !duplicateParams &&
    !invalidParam &&
    !writableMcpNeedsPolicy &&
    !emptyMcpMount &&
    !author.isPending;

  const mcpServerByName = useMemo(() => {
    return new Map((authoring?.mcp_servers ?? []).map((server) => [server.server, server]));
  }, [authoring?.mcp_servers]);

  function applyAuthorInput(input: WorkflowAuthorInput) {
    setName(input.name);
    setCard(input.card);
    setTools(input.tools);
    setParams(input.params);
    setMcpMounts(input.mcp_mounts);
    setSkills(input.skills);
    setAllowsWrites(input.allows_writes);
    setUntrustedContent(input.untrusted_content);
    setOutputPathTemplate(input.output_path_template);
    setOutputFormat(input.output_format);
    setVerifyChecks(input.verify_checks);
    setVerifyDraft("");
    setFinalizer(input.finalizer ?? "");
  }

  function addParam() {
    setParams((current) => [
      ...current,
      { name: "", type: DEFAULT_PARAM_TYPE, required: true, description: "" },
    ]);
  }

  function addMcpMount() {
    const firstServer = authoring?.mcp_servers[0];
    if (!firstServer) return;
    setMcpMounts((current) => [
      ...current,
      { server: firstServer.server, tool_names: [], read_only: true },
    ]);
  }

  function addVerifyChecks() {
    const next = compactTags(verifyDraft);
    if (next.length === 0) return;
    setVerifyChecks((current) => Array.from(new Set([...current, ...next])));
    setVerifyDraft("");
  }

  function handleVerifyKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key !== "Enter") return;
    event.preventDefault();
    addVerifyChecks();
  }

  async function handleDescribeSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canDraft) return;
    const cleaned = description.trim();
    const response = await draft.mutateAsync(cleaned);
    setResult(response);
    applyAuthorInput(authorInputFromReview(response.workflow));
    setAdvancedOpen(false);
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSubmit) return;
    const input: WorkflowAuthorInput = {
      name: name.trim(),
      card: card.trim(),
      params: params.map((param) => ({
        name: param.name.trim(),
        type: param.type,
        required: param.required,
        description: param.description?.trim() || undefined,
      })),
      tools,
      mcp_mounts: mcpMounts,
      skills,
      allows_writes: allowsWrites,
      untrusted_content: untrustedContent,
      output_path_template: outputPathTemplate.trim(),
      output_format: outputFormat,
      verify_checks: verifyChecks,
      finalizer: finalizer.trim() || null,
    };
    const response = await author.mutateAsync(input);
    setResult(resultFromAuthorDraft(response, description.trim()));
    setAdvancedOpen(false);
  }

  async function handleApprove() {
    if (!result) return;
    const approved = await approve.mutateAsync({
      instanceId: result.workflow.instance_id,
      surfaceShownDigest: result.workflow.surface_shown_digest,
    });
    setResult({ ...result, workflow: approved });
  }

  function handleEditDetails() {
    if (result) applyAuthorInput(authorInputFromReview(result.workflow));
    setAdvancedOpen(true);
  }

  function handleStartOver() {
    setDescription("");
    setResult(null);
    setAdvancedOpen(false);
    draft.reset();
    author.reset();
    approve.reset();
  }

  const errorFindings =
    result?.workflow.validation_findings.some((finding) => finding.severity === "error") ??
    false;
  const resultInput = result ? authorInputFromReview(result.workflow) : null;
  const uses = result ? workflowUses(result.workflow) : [];

  return (
    <section className="rounded-card border border-hairline bg-surface-1 p-body">
      <div className="flex flex-col gap-section">
        <form className="flex flex-col gap-inline" onSubmit={handleDescribeSubmit}>
          <div>
            <h2 className="font-ui text-[1rem] font-semibold text-text">Create workflow</h2>
          </div>
          <label className="flex flex-col gap-hair font-ui text-[0.8rem] text-text-muted">
            What should this workflow do?
            <textarea
              aria-label="What should this workflow do?"
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              rows={5}
              placeholder="Every morning, pull my unread emails, summarize them into 5 bullets, and save a markdown brief."
              className="rounded-control border border-hairline bg-surface-2 px-inline py-hair text-[0.84rem] text-text outline-none focus:border-accent"
            />
          </label>

          {draft.isError && (
            <div
              role="alert"
              className="flex items-start gap-hair rounded-control border border-unsupported/50 bg-surface-2 p-inline font-ui text-[0.82rem] text-unsupported"
            >
              <AlertTriangle className="size-4 shrink-0" aria-hidden />
              {errorMessage(draft.error, "Workflow drafting failed.")}
            </div>
          )}

          <div className="flex flex-wrap items-center gap-hair">
            <button
              type="submit"
              disabled={!canDraft}
              className="inline-flex w-fit items-center justify-center gap-hair rounded-control border border-hairline px-body py-hair font-ui text-[0.84rem] text-text-muted transition-colors hover:border-accent/60 hover:text-text disabled:cursor-not-allowed disabled:opacity-50"
            >
              {draft.isPending ? (
                <Loader2 className="size-3.5 animate-spin" aria-hidden />
              ) : (
                <ArrowRight className="size-3.5" aria-hidden />
              )}
              Draft it →
            </button>
            {!advancedOpen && !result && (
              <button
                type="button"
                onClick={() => setAdvancedOpen(true)}
                className="inline-flex w-fit items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted hover:text-text"
              >
                <Pencil className="size-3.5" aria-hidden />
                Edit details (Advanced)
              </button>
            )}
          </div>
        </form>

        {result && (
          <section className="flex flex-col gap-inline rounded-control border border-hairline bg-surface-2 p-inline">
            <div className="flex flex-col gap-hair">
              <div className="font-ui text-[0.82rem] text-text-muted">{result.summary}</div>
              <h3 className="font-ui text-[0.96rem] font-semibold text-text">
                {result.workflow.name}
              </h3>
              <dl className="grid gap-hair font-ui text-[0.8rem] text-text-muted md:grid-cols-[7rem_1fr]">
                <dt className="text-text-faint">Uses</dt>
                <dd className="text-text">{uses.length > 0 ? uses.join(", ") : "No tools"}</dd>
                <dt className="text-text-faint">Produces</dt>
                <dd className="text-text">
                  {outputLabel(result)} ({result.simulation.output_format ?? "unknown"})
                </dd>
                <dt className="text-text-faint">Asks for</dt>
                <dd className="text-text">
                  {resultInput && resultInput.params.length > 0
                    ? resultInput.params
                        .map((param) => `${param.name}${param.required ? "" : " (optional)"}`)
                        .join(", ")
                    : "No parameters"}
                </dd>
              </dl>
            </div>

            <section className="flex flex-col gap-hair">
              <h4 className="font-ui text-[0.86rem] font-semibold text-text">
                Validation findings
              </h4>
              <FindingRows findings={result.workflow.validation_findings} />
              {errorFindings && (
                <div className="font-ui text-[0.78rem] text-unsupported">
                  Error findings block approval.
                </div>
              )}
            </section>

            <div className="flex items-center gap-hair font-ui text-[0.8rem] text-text-muted">
              {result.simulation.ok ? (
                <CheckCircle2 className="size-4 text-supported" aria-hidden />
              ) : (
                <AlertTriangle className="size-4 text-warn" aria-hidden />
              )}
              Simulation {result.simulation.ok ? "ok" : "needs attention"}
            </div>

            {approve.isError && (
              <div
                role="alert"
                className="flex items-start gap-hair rounded-control border border-unsupported/50 bg-surface-1 p-inline font-ui text-[0.82rem] text-unsupported"
              >
                <AlertTriangle className="size-4 shrink-0" aria-hidden />
                {errorMessage(approve.error, "Workflow approval failed.")}
              </div>
            )}

            <div className="flex flex-wrap items-center gap-hair">
              <button
                type="button"
                onClick={handleApprove}
                disabled={errorFindings || approve.isPending || result.workflow.approved}
                className="inline-flex w-fit items-center justify-center gap-hair rounded-control border border-hairline px-body py-hair font-ui text-[0.84rem] text-text-muted transition-colors hover:border-accent/60 hover:text-text disabled:cursor-not-allowed disabled:opacity-50"
              >
                {approve.isPending ? (
                  <Loader2 className="size-3.5 animate-spin" aria-hidden />
                ) : (
                  <Check className="size-3.5" aria-hidden />
                )}
                {result.workflow.approved ? "Approved" : "Approve"}
              </button>
              <button
                type="button"
                onClick={handleEditDetails}
                className="inline-flex w-fit items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted hover:text-text"
              >
                <Pencil className="size-3.5" aria-hidden />
                Edit details
              </button>
              <button
                type="button"
                onClick={handleStartOver}
                className="inline-flex w-fit items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted hover:text-text"
              >
                <RotateCcw className="size-3.5" aria-hidden />
                Start over
              </button>
            </div>
          </section>
        )}

        {advancedOpen && (
          <section className="flex flex-col gap-inline border-t border-hairline pt-section">
            <div className="flex items-center justify-between gap-inline">
              <h3 className="font-ui text-[0.92rem] font-semibold text-text">
                Edit details (Advanced)
              </h3>
              {result && (
                <button
                  type="button"
                  onClick={() => setAdvancedOpen(false)}
                  className="inline-flex w-fit items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted hover:text-text"
                >
                  <X className="size-3.5" aria-hidden />
                  Close
                </button>
              )}
            </div>

            {context.isLoading && (
              <div className="flex items-center gap-hair font-ui text-[0.86rem] text-text-muted">
                <Loader2 className="size-4 animate-spin" aria-hidden />
                Loading authoring context.
              </div>
            )}

            {(context.isError || (!context.isLoading && !authoring)) && (
              <div
                role="alert"
                className="rounded-control border border-hairline border-l-2 border-l-warn bg-surface-2 p-inline font-ui text-[0.86rem] text-text"
              >
                Could not load workflow authoring context.
              </div>
            )}

            {authoring && (
              <form className="flex flex-col gap-section" onSubmit={handleSubmit}>
                <div className="grid gap-inline md:grid-cols-[minmax(0,18rem)_1fr]">
                  <label className="flex flex-col gap-hair font-ui text-[0.8rem] text-text-muted">
                    Name
                    <input
                      aria-label="Name"
                      value={name}
                      onChange={(event) => setName(event.target.value)}
                      className="rounded-control border border-hairline bg-surface-2 px-inline py-hair text-[0.84rem] text-text outline-none focus:border-accent"
                    />
                  </label>
                  <label className="flex flex-col gap-hair font-ui text-[0.8rem] text-text-muted">
                    Card
                    <textarea
                      aria-label="Card"
                      value={card}
                      onChange={(event) => setCard(event.target.value)}
                      rows={3}
                      className="rounded-control border border-hairline bg-surface-2 px-inline py-hair text-[0.84rem] text-text outline-none focus:border-accent"
                    />
                    <span className={cardHasBlankLine ? "text-unsupported" : "text-text-faint"}>
                      One paragraph only; blank lines are rejected.
                    </span>
                  </label>
                </div>

                <section className="flex flex-col gap-inline">
                  <h4 className="font-ui text-[0.86rem] font-semibold text-text">Tools</h4>
                  <div className="grid gap-hair md:grid-cols-2">
                    {authoring.builtin_tools.map((tool) => (
                      <label
                        key={tool.name}
                        className="flex items-start gap-hair rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.8rem] text-text"
                      >
                        <input
                          type="checkbox"
                          checked={tools.includes(tool.name)}
                          onChange={(event) =>
                            setTools((current) =>
                              toggleValue(current, tool.name, event.target.checked),
                            )
                          }
                          className="mt-[0.2rem]"
                        />
                        <span className="min-w-0">
                          <span className="flex flex-wrap items-center gap-hair">
                            <span className="font-semibold">{tool.name}</span>
                            <span className="rounded-full border border-hairline px-hair text-[0.68rem] text-text-muted">
                              {tool.read_only ? "read-only" : "writes"}
                            </span>
                          </span>
                          <span className="block text-text-muted">{tool.description}</span>
                        </span>
                      </label>
                    ))}
                  </div>
                </section>

                <section className="flex flex-col gap-inline">
                  <div className="flex items-center justify-between gap-inline">
                    <h4 className="font-ui text-[0.86rem] font-semibold text-text">
                      Parameters
                    </h4>
                    <button
                      type="button"
                      onClick={addParam}
                      className="inline-flex items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted hover:text-text"
                    >
                      <Plus className="size-3.5" aria-hidden />
                      Add
                    </button>
                  </div>
                  <div className="flex flex-col gap-hair">
                    {params.map((param, index) => (
                      <div
                        key={index}
                        className="grid gap-hair md:grid-cols-[1fr_10rem_6rem_1fr_auto]"
                      >
                        <input
                          aria-label={`Parameter ${index + 1} name`}
                          value={param.name}
                          onChange={(event) =>
                            setParams((current) =>
                              current.map((item, itemIndex) =>
                                itemIndex === index
                                  ? { ...item, name: event.target.value }
                                  : item,
                              ),
                            )
                          }
                          placeholder="name"
                          className="rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.82rem] text-text outline-none focus:border-accent"
                        />
                        <select
                          aria-label={`Parameter ${index + 1} type`}
                          value={param.type}
                          onChange={(event) =>
                            setParams((current) =>
                              current.map((item, itemIndex) =>
                                itemIndex === index
                                  ? { ...item, type: event.target.value as ParamType }
                                  : item,
                              ),
                            )
                          }
                          className="rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.82rem] text-text outline-none focus:border-accent"
                        >
                          {authoring.param_types.map((type) => (
                            <option key={type} value={type}>
                              {type}
                            </option>
                          ))}
                        </select>
                        <label className="flex items-center gap-hair rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.78rem] text-text-muted">
                          <input
                            type="checkbox"
                            checked={param.required}
                            onChange={(event) =>
                              setParams((current) =>
                                current.map((item, itemIndex) =>
                                  itemIndex === index
                                    ? { ...item, required: event.target.checked }
                                    : item,
                                ),
                              )
                            }
                          />
                          Required
                        </label>
                        <input
                          aria-label={`Parameter ${index + 1} description`}
                          value={param.description ?? ""}
                          onChange={(event) =>
                            setParams((current) =>
                              current.map((item, itemIndex) =>
                                itemIndex === index
                                  ? { ...item, description: event.target.value }
                                  : item,
                              ),
                            )
                          }
                          placeholder="description"
                          className="rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.82rem] text-text outline-none focus:border-accent"
                        />
                        <button
                          type="button"
                          onClick={() =>
                            setParams((current) => current.filter((_, i) => i !== index))
                          }
                          aria-label={`Remove parameter ${index + 1}`}
                          className="inline-flex items-center justify-center rounded-control border border-hairline px-inline py-hair text-text-muted hover:text-unsupported"
                        >
                          <Trash2 className="size-4" aria-hidden />
                        </button>
                      </div>
                    ))}
                  </div>
                  {(duplicateParams || invalidParam) && (
                    <div className="font-ui text-[0.78rem] text-unsupported">
                      Parameter names must be present and unique.
                    </div>
                  )}
                </section>

                {authoring.mcp_servers.length > 0 && (
                  <details className="rounded-control border border-hairline bg-surface-2 p-inline">
                    <summary className="cursor-pointer font-ui text-[0.86rem] font-semibold text-text">
                      MCP mounts
                    </summary>
                    <div className="mt-inline flex flex-col gap-inline">
                      {mcpMounts.map((mount, index) => {
                        const server = mcpServerByName.get(mount.server);
                        return (
                          <div
                            key={index}
                            className="flex flex-col gap-hair border-t border-hairline pt-inline"
                          >
                            <div className="grid gap-hair md:grid-cols-[14rem_1fr_auto]">
                              <select
                                aria-label={`MCP mount ${index + 1} server`}
                                value={mount.server}
                                onChange={(event) => {
                                  const nextServer = event.target.value;
                                  setMcpMounts((current) =>
                                    current.map((item, itemIndex) =>
                                      itemIndex === index
                                        ? { ...item, server: nextServer, tool_names: [] }
                                        : item,
                                    ),
                                  );
                                }}
                                className="rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.82rem] text-text outline-none focus:border-accent"
                              >
                                {authoring.mcp_servers.map((item) => (
                                  <option key={item.server} value={item.server}>
                                    {item.server}
                                  </option>
                                ))}
                              </select>
                              <div className="flex flex-wrap gap-hair">
                                {(server?.tools ?? []).map((tool) => (
                                  <label
                                    key={tool}
                                    className="flex items-center gap-hair rounded-control border border-hairline bg-surface-1 px-inline py-hair font-ui text-[0.78rem] text-text-muted"
                                  >
                                    <input
                                      type="checkbox"
                                      checked={mount.tool_names.includes(tool)}
                                      onChange={(event) =>
                                        setMcpMounts((current) =>
                                          current.map((item, itemIndex) =>
                                            itemIndex === index
                                              ? {
                                                  ...item,
                                                  tool_names: toggleValue(
                                                    item.tool_names,
                                                    tool,
                                                    event.target.checked,
                                                  ),
                                                }
                                              : item,
                                          ),
                                        )
                                      }
                                    />
                                    {tool}
                                  </label>
                                ))}
                              </div>
                              <button
                                type="button"
                                onClick={() =>
                                  setMcpMounts((current) =>
                                    current.filter((_, i) => i !== index),
                                  )
                                }
                                aria-label={`Remove MCP mount ${index + 1}`}
                                className="inline-flex items-center justify-center rounded-control border border-hairline px-inline py-hair text-text-muted hover:text-unsupported"
                              >
                                <Trash2 className="size-4" aria-hidden />
                              </button>
                            </div>
                            <label className="flex items-center gap-hair font-ui text-[0.8rem] text-text-muted">
                              <input
                                type="checkbox"
                                checked={mount.read_only}
                                onChange={(event) =>
                                  setMcpMounts((current) =>
                                    current.map((item, itemIndex) =>
                                      itemIndex === index
                                        ? { ...item, read_only: event.target.checked }
                                        : item,
                                    ),
                                  )
                                }
                              />
                              Read-only mount
                            </label>
                          </div>
                        );
                      })}
                      <button
                        type="button"
                        onClick={addMcpMount}
                        className="inline-flex w-fit items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted hover:text-text"
                      >
                        <Plus className="size-3.5" aria-hidden />
                        Add MCP mount
                      </button>
                      {(emptyMcpMount || writableMcpNeedsPolicy) && (
                        <div className="font-ui text-[0.78rem] text-unsupported">
                          Pick at least one tool per mount; writable mounts require Allows writes.
                        </div>
                      )}
                    </div>
                  </details>
                )}

                {authoring.skills.length > 0 && (
                  <section className="flex flex-col gap-inline">
                    <h4 className="font-ui text-[0.86rem] font-semibold text-text">
                      Skills
                    </h4>
                    <div className="flex flex-wrap gap-hair">
                      {authoring.skills.map((skill) => (
                        <label
                          key={skill}
                          className="flex items-center gap-hair rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.78rem] text-text-muted"
                        >
                          <input
                            type="checkbox"
                            checked={skills.includes(skill)}
                            onChange={(event) =>
                              setSkills((current) =>
                                toggleValue(current, skill, event.target.checked),
                              )
                            }
                          />
                          {skill}
                        </label>
                      ))}
                    </div>
                  </section>
                )}

                <section className="grid gap-inline md:grid-cols-[1fr_12rem]">
                  <label className="flex flex-col gap-hair font-ui text-[0.8rem] text-text-muted">
                    Output path template
                    <input
                      value={outputPathTemplate}
                      onChange={(event) => setOutputPathTemplate(event.target.value)}
                      className="rounded-control border border-hairline bg-surface-2 px-inline py-hair text-[0.84rem] text-text outline-none focus:border-accent"
                    />
                    <span className="text-text-faint">Example: outputs/{"{param}"}.md</span>
                  </label>
                  <label className="flex flex-col gap-hair font-ui text-[0.8rem] text-text-muted">
                    Format
                    <select
                      value={outputFormat}
                      onChange={(event) => setOutputFormat(event.target.value as OutputFormat)}
                      className="rounded-control border border-hairline bg-surface-2 px-inline py-hair text-[0.84rem] text-text outline-none focus:border-accent"
                    >
                      {authoring.output_formats.map((format) => (
                        <option key={format} value={format}>
                          {format}
                        </option>
                      ))}
                    </select>
                  </label>
                </section>

                <section className="grid gap-inline md:grid-cols-2">
                  <div className="flex flex-col gap-hair">
                    <label className="font-ui text-[0.8rem] text-text-muted">
                      Verify checks
                      <input
                        value={verifyDraft}
                        onChange={(event) => setVerifyDraft(event.target.value)}
                        onKeyDown={handleVerifyKeyDown}
                        className="mt-hair w-full rounded-control border border-hairline bg-surface-2 px-inline py-hair text-[0.84rem] text-text outline-none focus:border-accent"
                      />
                    </label>
                    <button
                      type="button"
                      onClick={addVerifyChecks}
                      className="inline-flex w-fit items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted hover:text-text"
                    >
                      <Plus className="size-3.5" aria-hidden />
                      Add check
                    </button>
                    {verifyChecks.length > 0 && (
                      <div className="flex flex-wrap gap-hair">
                        {verifyChecks.map((check) => (
                          <span
                            key={check}
                            className="inline-flex items-center gap-hair rounded-full border border-hairline px-inline py-px font-ui text-[0.72rem] text-text-muted"
                          >
                            {check}
                            <button
                              type="button"
                              onClick={() =>
                                setVerifyChecks((current) =>
                                  current.filter((item) => item !== check),
                                )
                              }
                              aria-label={`Remove verify check ${check}`}
                              className="text-text-faint hover:text-unsupported"
                            >
                              <X className="size-3" aria-hidden />
                            </button>
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                  <label className="flex flex-col gap-hair font-ui text-[0.8rem] text-text-muted">
                    Finalizer
                    <input
                      value={finalizer}
                      onChange={(event) => setFinalizer(event.target.value)}
                      className="rounded-control border border-hairline bg-surface-2 px-inline py-hair text-[0.84rem] text-text outline-none focus:border-accent"
                    />
                  </label>
                </section>

                <section className="grid gap-hair md:grid-cols-2">
                  <label className="flex items-start gap-hair rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.8rem] text-text-muted">
                    <input
                      type="checkbox"
                      checked={allowsWrites}
                      onChange={(event) => setAllowsWrites(event.target.checked)}
                      className="mt-[0.2rem]"
                    />
                    <span>
                      <span className="block text-text">Allows writes</span>
                      <span>Required if any MCP mount is writable.</span>
                    </span>
                  </label>
                  <label className="flex items-start gap-hair rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.8rem] text-text-muted">
                    <input
                      type="checkbox"
                      checked={untrustedContent}
                      onChange={(event) => setUntrustedContent(event.target.checked)}
                      className="mt-[0.2rem]"
                    />
                    <span>
                      <span className="block text-text">Untrusted content</span>
                      <span>Default on for user and connector-provided inputs.</span>
                    </span>
                  </label>
                </section>

                {author.isError && (
                  <div
                    role="alert"
                    className="flex items-start gap-hair rounded-control border border-unsupported/50 bg-surface-2 p-inline font-ui text-[0.82rem] text-unsupported"
                  >
                    <AlertTriangle className="size-4 shrink-0" aria-hidden />
                    {errorMessage(author.error, "Workflow authoring failed.")}
                  </div>
                )}

                <button
                  type="submit"
                  disabled={!canSubmit}
                  className="inline-flex w-fit items-center justify-center gap-hair rounded-control border border-hairline px-body py-hair font-ui text-[0.84rem] text-text-muted transition-colors hover:border-accent/60 hover:text-text disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {author.isPending ? (
                    <Loader2 className="size-3.5 animate-spin" aria-hidden />
                  ) : (
                    <Plus className="size-3.5" aria-hidden />
                  )}
                  Create draft
                </button>
              </form>
            )}
          </section>
        )}
      </div>
    </section>
  );
}
