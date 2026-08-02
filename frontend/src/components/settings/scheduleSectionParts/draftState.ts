/**
 * The in-progress "new schedule" draft shape for ScheduleSection — relocated
 * out of the parent (still private, non-exported implementation detail) so
 * ScheduleComposer can share it.
 */

export interface DraftState {
  input: string; // raw NL input from the user
  description: string | null;
}

export const EMPTY_DRAFT: DraftState = { input: "", description: null };
