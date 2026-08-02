// Cross-hook boolean composition for the author form. These combine fields
// owned by different `useAuthorX` hooks (draft fields, params, mcp mounts) and
// live here — as plain functions, not inline expressions in a component — so
// their branch count is charged to this module instead of the composing
// component's cyclomatic complexity budget.

export function canDraftAuthorForm(description: string, isDrafting: boolean): boolean {
  return Boolean(description.trim()) && !isDrafting;
}

export function mcpWritesNeedPolicy(hasWritableMount: boolean, allowsWrites: boolean): boolean {
  return hasWritableMount && !allowsWrites;
}

export interface CanSubmitAuthorFormInput {
  hasAuthoringContext: boolean;
  name: string;
  card: string;
  outputPathTemplate: string;
  cardHasBlankLine: boolean;
  duplicateParams: boolean;
  invalidParam: boolean;
  writableMcpNeedsPolicy: boolean;
  emptyMcpMount: boolean;
  isSubmitting: boolean;
}

export function canSubmitAuthorForm(input: CanSubmitAuthorFormInput): boolean {
  return (
    input.hasAuthoringContext &&
    Boolean(input.name.trim()) &&
    Boolean(input.card.trim()) &&
    Boolean(input.outputPathTemplate.trim()) &&
    !input.cardHasBlankLine &&
    !input.duplicateParams &&
    !input.invalidParam &&
    !input.writableMcpNeedsPolicy &&
    !input.emptyMcpMount &&
    !input.isSubmitting
  );
}
