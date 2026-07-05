import type { Mode } from "@/shell/mode";

const MODES: readonly Mode[] = ["search", "build", "agent"];

const freshModes = new Set<Mode>();
const killedConversationIds = new Set<string>();

export function markAllModesFresh(): void {
  for (const mode of MODES) freshModes.add(mode);
}

export function markFreshMode(mode: Mode): void {
  freshModes.add(mode);
}

export function clearFreshMode(mode: Mode): void {
  freshModes.delete(mode);
}

export function isFreshMode(mode: Mode): boolean {
  return freshModes.has(mode);
}

export function markConversationKilled(conversationId: string | null | undefined): void {
  if (conversationId) killedConversationIds.add(conversationId);
}

export function isConversationKilled(conversationId: string | null | undefined): boolean {
  return Boolean(conversationId && killedConversationIds.has(conversationId));
}

export function resetSessionResumeStateForTests(): void {
  freshModes.clear();
  killedConversationIds.clear();
}
