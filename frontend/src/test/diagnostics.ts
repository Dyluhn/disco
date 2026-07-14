const FORBIDDEN_TEST_DIAGNOSTICS = [
  /not wrapped in act\(\.\.\.\)/i,
  /suspended inside an [`'“”]?act/i,
  /called act\(async \(\) => \.\.\.\) without await/i,
  /act call was not awaited/i,
  /not implemented: navigation/i,
];

function diagnosticText(args: unknown[]): string {
  return args
    .map((arg) => (arg instanceof Error ? `${arg.name}: ${arg.message}` : String(arg)))
    .join(" ");
}

/** Return the diagnostic text only when a passing test would otherwise hide an
 * async React boundary violation or unsupported jsdom navigation attempt. */
export function forbiddenTestDiagnostic(args: unknown[]): string | null {
  const text = diagnosticText(args);
  return FORBIDDEN_TEST_DIAGNOSTICS.some((pattern) => pattern.test(text))
    ? text
    : null;
}
