/** A local "is the pasted workflow even valid JSON?" check so the user gets an
 * honest hint BEFORE the agent's next image-gen fails server-side. Empty = use
 * the built-in default (not an error). Tokens are substituted at gen time, so a
 * template with %seed% etc. won't parse here — only flag clearly-broken braces. */
export function getWorkflowJsonError(workflowJson: string): string | null {
  const t = workflowJson.trim();
  if (!t) return null;
  // Numeric tokens are substituted as BARE numbers, so they must NOT sit inside
  // quotes — `"seed": "%seed%"` would send `"123"` (a string) and ComfyUI rejects it.
  // Catch that here since the parse-probe below would otherwise mask it.
  if (/"\s*%(seed|width|height)%\s*"/.test(t))
    return 'Numeric tokens (%seed% %width% %height%) must be UNQUOTED, e.g. "seed": %seed% — not "%seed%".';
  // Strip the substitution tokens to a parseable stand-in before validating shape.
  const probe = t
    .replace(/%seed%|%width%|%height%/g, "0")
    .replace(/%prompt%|%negative%|%ckpt%/g, "x");
  try {
    const parsed = JSON.parse(probe);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed))
      return "Workflow must be a JSON object of nodes (ComfyUI 'Save (API Format)').";
    return null;
  } catch {
    return "Not valid JSON yet — paste a ComfyUI 'Save (API Format)' export.";
  }
}
