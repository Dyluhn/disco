export type CleanupAssessment = {
  attempted: number;
  failed: number;
};

/** Transfer ownership immediately after creation, before any later operation can fail. */
export async function createRegisteredTarget<T>(
  create: () => Promise<T>,
  register: (target: T) => void | Promise<void>,
  start: (target: T) => Promise<void>,
): Promise<T> {
  const target = await create();
  await register(target);
  await start(target);
  return target;
}

/** Attempt every cleanup target without retaining target identities or error bodies. */
export async function attemptAllCleanup<T>(
  targets: readonly T[],
  remove: (target: T) => Promise<void>,
): Promise<CleanupAssessment> {
  let failed = 0;
  for (const target of [...targets].reverse()) {
    try {
      await remove(target);
    } catch {
      failed += 1;
    }
  }
  return { attempted: targets.length, failed };
}

/** Preserve the original failure even if its supplemental evidence cannot be attached. */
export async function rethrowAfterBestEffortReport(
  primaryError: unknown,
  report: () => Promise<void>,
): Promise<never> {
  try {
    await report();
  } catch {
    // The caller must first record a deterministic annotation. This attachment is
    // supplemental and can never replace the original product/test failure.
  }
  throw primaryError;
}
