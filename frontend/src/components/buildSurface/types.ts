/**
 * Shared types for the BuildSurface.tsx decomposition (PKG-12-FE-BUILD): every
 * subcomponent/hook under this directory takes the full `useBuild()` result as
 * its `b` prop rather than re-deriving a narrower shape, so prop plumbing stays
 * mechanical and the useBuild return contract only has one source of truth.
 */

import type { useBuild } from "@/hooks/useBuild";

/** The full Build controller returned by `useBuild()`. */
export type BuildController = ReturnType<typeof useBuild>;

/** The per-framing ("build" vs "agent") copy strings BuildSurface picks via
 *  its private FRAMING record and threads down to the subcomponents that need
 *  them (the empty state and the re-plan composer). */
export interface FramingCopy {
  placeholder: string;
  replanPlaceholder: string;
  startError: string;
  heroTitle: string;
  heroSubtitle: string;
}
