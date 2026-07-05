import { AlertTriangle, CheckCircle2, Loader2, Plus, Trash2, X } from "lucide-react";
import { useMemo, useState } from "react";
import type { FormEvent, KeyboardEvent } from "react";
import { useAuthoringContext, useAuthorWorkflow } from "@/hooks/useWorkflows";
import type {
  WorkflowAuthorInput,
  WorkflowAuthoringContext,
  WorkflowDraftParam,
  WorkflowDraftResult,
  WorkflowMcpMountInput,
  WorkflowValidationFinding,
} from "@/types/workflow";

type ParamType = WorkflowAuthoringContext["param_types"][number];
type OutputFormat = WorkflowAuthoringContext["output_formats"][number];

const DEFAULT_PARAM_TYPE: ParamType = "string";
const DEFAULT_OUTPUT_FORMAT: OutputFormat = "markdown";

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
  const [result, setResult] = useState<WorkflowDraftResult | null>(null);
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
  const canSubmit =
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
    setResult(response);
  }

  if (context.isLoading) {
    return (
      <section className="rounded-card border border-hairline bg-surface-1 p-body">
        <div className="flex items-center gap-hair font-ui text-[0.86rem] text-text-muted">
          <Loader2 className="size-4 animate-spin" aria-hidden />
          Loading authoring context.
        </div>
      </section>
    );
  }

  if (context.isError || !authoring) {
    return (
      <section
        role="alert"
        className="rounded-card border border-hairline border-l-2 border-l-warn bg-surface-1 p-body font-ui text-[0.86rem] text-text"
      >
        Could not load workflow authoring context.
      </section>
    );
  }

  return (
    <section className="rounded-card border border-hairline bg-surface-1 p-body">
      <form className="flex flex-col gap-section" onSubmit={handleSubmit}>
        <div>
          <h2 className="font-ui text-[1rem] font-semibold text-text">Create Workflow</h2>
          <p className="font-ui text-[0.82rem] text-text-muted">
            Choose from the live tool, MCP, skill, and parameter inventory.
          </p>
        </div>

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
          <h3 className="font-ui text-[0.86rem] font-semibold text-text">Tools</h3>
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
                    setTools((current) => toggleValue(current, tool.name, event.target.checked))
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
            <h3 className="font-ui text-[0.86rem] font-semibold text-text">Parameters</h3>
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
              <div key={index} className="grid gap-hair md:grid-cols-[1fr_10rem_6rem_1fr_auto]">
                <input
                  aria-label={`Parameter ${index + 1} name`}
                  value={param.name}
                  onChange={(event) =>
                    setParams((current) =>
                      current.map((item, itemIndex) =>
                        itemIndex === index ? { ...item, name: event.target.value } : item,
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
                        itemIndex === index ? { ...item, description: event.target.value } : item,
                      ),
                    )
                  }
                  placeholder="description"
                  className="rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.82rem] text-text outline-none focus:border-accent"
                />
                <button
                  type="button"
                  onClick={() => setParams((current) => current.filter((_, i) => i !== index))}
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
                  <div key={index} className="flex flex-col gap-hair border-t border-hairline pt-inline">
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
                          setMcpMounts((current) => current.filter((_, i) => i !== index))
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
            <h3 className="font-ui text-[0.86rem] font-semibold text-text">Skills</h3>
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
                      setSkills((current) => toggleValue(current, skill, event.target.checked))
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
                        setVerifyChecks((current) => current.filter((item) => item !== check))
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
            Workflow authoring failed.
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

      {result && (
        <div className="mt-section flex flex-col gap-inline border-t border-hairline pt-section">
          <section className="flex flex-col gap-hair">
            <h3 className="font-ui text-[0.86rem] font-semibold text-text">
              Validation findings
            </h3>
            <FindingRows findings={result.workflow.validation_findings} />
            {result.workflow.validation_findings.some((finding) => finding.severity === "error") && (
              <div className="font-ui text-[0.78rem] text-unsupported">
                Error findings block approval.
              </div>
            )}
          </section>
          <section className="rounded-control border border-hairline bg-surface-2 p-inline font-ui text-[0.82rem] text-text-muted">
            <h3 className="text-[0.86rem] font-semibold text-text">Simulation result</h3>
            <div>ok: {String(result.simulation.ok)}</div>
            <div>output_path: {result.simulation.output_path ?? "none"}</div>
            <div>fixture_bytes: {result.simulation.fixture_bytes}</div>
          </section>
        </div>
      )}
    </section>
  );
}
