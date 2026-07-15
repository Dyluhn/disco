function redactText(value: string, identifiers: readonly string[]): string {
  let redacted = value;
  for (const identifier of identifiers) {
    if (!identifier) continue;
    redacted = redacted.replaceAll(identifier, "[REDACTED_CONVERSATION_ID]");
    const compact = identifier.replace(/^conv_/, "").slice(0, 8);
    if (compact) redacted = redacted.replaceAll(compact, "[REDACTED_CID_PREFIX]");
  }
  return redacted;
}

/** Redact every retained failure string while preserving Error identity when mutable. */
export function redactFailureStringsInPlace(
  failure: unknown,
  redact: (value: string) => string,
): unknown {
  const seen = new WeakSet<object>();
  const visit = (value: unknown, depth: number): unknown => {
    if (typeof value === "string") return redact(value);
    if (value === null || typeof value !== "object") return value;
    if (depth > 8) return "[REDACTED_DEEP_FAILURE_EVIDENCE]";
    if (seen.has(value)) return value;
    seen.add(value);
    let immutable = false;
    for (const key of Object.keys(value)) {
      try {
        const record = value as Record<string, unknown>;
        record[key] = visit(record[key], depth + 1);
      } catch {
        immutable = true;
      }
    }
    if (value instanceof Error) {
      const message = redact(value.message);
      const stack = value.stack ? redact(value.stack) : undefined;
      const cause = visit((value as Error & { cause?: unknown }).cause, depth + 1);
      try {
        value.name = redact(value.name);
        value.message = message;
        if (stack) value.stack = stack;
        if ("cause" in value) (value as Error & { cause?: unknown }).cause = cause;
      } catch {
        immutable = true;
      }
      if (immutable) {
        const fallback = new Error(message, cause === undefined ? undefined : { cause });
        fallback.name = redact(value.name);
        if (stack) fallback.stack = stack;
        return fallback;
      }
    } else if (immutable) {
      return new Error("immutable non-Error failure evidence was redacted");
    }
    return value;
  };
  try {
    return visit(failure, 0);
  } catch {
    return new Error("failure evidence redaction failed closed");
  }
}

/** Redact known identifiers throughout a failure while preserving Error identity. */
export function redactKnownIdentifiersInPlace(
  failure: unknown,
  identifiers: readonly string[],
): unknown {
  return redactFailureStringsInPlace(failure, (value) => redactText(value, identifiers));
}

export function redactKnownIdentifiers(
  value: string,
  identifiers: readonly string[],
): string {
  return redactText(value, identifiers);
}
