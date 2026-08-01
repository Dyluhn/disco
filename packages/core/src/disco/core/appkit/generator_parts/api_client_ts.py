"""The generated SPA's `src/api/client.ts` + `src/hooks/useSubmit.ts` emitters.

Split out of `..generator` (verbatim) to keep that module under the
`python_or_harness_module_logical_gt_700` budget. See `generator_parts/__init__.py`.
"""

from __future__ import annotations


def _emit_api_client_ts() -> str:
    return (
        "export type ApiResult<T> =\n"
        "  | { ok: true; data: T }\n"
        "  | { ok: false; status: number; error: string };\n\n"
        "function errorFromBody(body: unknown): string | null {\n"
        '  if (typeof body !== "object" || body === null || !("error" in body)) {\n'
        "    return null;\n"
        "  }\n"
        "  const value = (body as { error: unknown }).error;\n"
        '  return typeof value === "string" && value.trim() ? value : null;\n'
        "}\n\n"
        "async function readError(response: Response): Promise<string> {\n"
        "  try {\n"
        "    const body: unknown = await response.json();\n"
        '    return (errorFromBody(body) ?? response.statusText) || "Request failed";\n'
        "  } catch {\n"
        '    return response.statusText || "Request failed";\n'
        "  }\n"
        "}\n\n"
        "export async function postJson<T>(path: string, body: unknown): Promise<ApiResult<T>> {\n"
        "  try {\n"
        "    const response = await fetch(path, {\n"
        '      method: "POST",\n'
        '      headers: { "Content-Type": "application/json" },\n'
        "      body: JSON.stringify(body),\n"
        "    });\n"
        "    if (!response.ok) {\n"
        "      return { ok: false, status: response.status, error: await readError(response) };\n"
        "    }\n"
        "    const data = (await response.json()) as T;\n"
        "    return { ok: true, data };\n"
        "  } catch (error: unknown) {\n"
        "    return {\n"
        "      ok: false,\n"
        "      status: 0,\n"
        '      error: error instanceof Error ? error.message : "Network error",\n'
        "    };\n"
        "  }\n"
        "}\n"
    )


def _emit_submit_hook_ts() -> str:
    return (
        'import { useCallback, useRef, useState } from "react";\n'
        'import { postJson } from "../api/client";\n\n'
        "export type SubmitState =\n"
        '  | { kind: "idle" }\n'
        '  | { kind: "submitting" }\n'
        '  | { kind: "success" }\n'
        '  | { kind: "error"; message: string };\n\n'
        "export interface SubmittedEntry {\n"
        "  id: string;\n"
        "  values: Record<string, string>;\n"
        "}\n\n"
        "function entryId(): string {\n"
        "  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;\n"
        "}\n\n"
        "export function useSubmit(path: string) {\n"
        '  const [state, setState] = useState<SubmitState>({ kind: "idle" });\n'
        "  const [submitted, setSubmitted] = useState<SubmittedEntry[]>([]);\n"
        "  const inFlight = useRef(false);\n\n"
        "  const submit = useCallback(\n"
        "    async (values: Record<string, string>): Promise<boolean> => {\n"
        "      if (inFlight.current) return false;\n"
        "      inFlight.current = true;\n"
        '      setState({ kind: "submitting" });\n'
        "      const entry: SubmittedEntry = { id: entryId(), values: { ...values } };\n"
        "      setSubmitted((current) => [entry, ...current]);\n\n"
        "      const result = await postJson<{ ok: true }>(path, values);\n"
        "      inFlight.current = false;\n"
        "      if (result.ok) {\n"
        '        setState({ kind: "success" });\n'
        "        return true;\n"
        "      }\n"
        "      setSubmitted((current) => current.filter((item) => item.id !== entry.id));\n"
        '      setState({ kind: "error", message: result.error });\n'
        "      return false;\n"
        "    },\n"
        "    [path]\n"
        "  );\n\n"
        "  const reset = useCallback(() => {\n"
        '    setState({ kind: "idle" });\n'
        "  }, []);\n\n"
        "  return { state, submitted, submit, reset };\n"
        "}\n"
    )
