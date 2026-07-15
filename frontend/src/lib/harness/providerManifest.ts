import fs from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";

export const PROVIDER_CONVERSATION_MANIFEST_ENV =
  "DISCO_RELIABILITY_PROVIDER_CONVERSATION_MANIFEST";

type Manifest = {
  schema_version: 1;
  conversation_ids: string[];
};

function validateIds(conversationIds: readonly string[]): void {
  if (conversationIds.length < 1 || conversationIds.length > 2)
    throw new Error(
      "provider manifest must contain one or two conversation IDs",
    );
  if (new Set(conversationIds).size !== conversationIds.length)
    throw new Error("provider manifest conversation IDs must be unique");
  if (
    !conversationIds.every(
      (cid) => typeof cid === "string" && cid.length > 0 && cid === cid.trim(),
    )
  )
    throw new Error(
      "provider manifest conversation IDs must be exact nonempty strings",
    );
}

function parseExisting(data: string): Manifest {
  const parsed = JSON.parse(data) as unknown;
  if (
    parsed === null ||
    typeof parsed !== "object" ||
    Array.isArray(parsed) ||
    Object.keys(parsed).sort().join(",") !== "conversation_ids,schema_version"
  )
    throw new Error("existing provider manifest is malformed");
  const record = parsed as Record<string, unknown>;
  if (
    record.schema_version !== 1 ||
    typeof record.schema_version !== "number" ||
    !Array.isArray(record.conversation_ids)
  )
    throw new Error("existing provider manifest is malformed");
  validateIds(record.conversation_ids as string[]);
  return record as Manifest;
}

function openExistingManifest(resolvedManifest: string): {
  descriptor: number;
  manifest: Manifest;
} | null {
  let descriptor: number;
  try {
    descriptor = fs.openSync(
      resolvedManifest,
      fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK,
    );
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw error;
  }
  try {
    const stats = fs.fstatSync(descriptor);
    if (!stats.isFile())
      throw new Error("existing provider manifest must be a regular file");
    if (stats.size > 16 * 1024)
      throw new Error("existing provider manifest is unreasonably large");
    return {
      descriptor,
      manifest: parseExisting(fs.readFileSync(descriptor, "utf-8")),
    };
  } catch (error) {
    fs.closeSync(descriptor);
    throw error;
  }
}

function syncDirectory(directory: string): void {
  const descriptor = fs.openSync(directory, fs.constants.O_RDONLY);
  try {
    fs.fsyncSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
}

/** Persist exact provider scope after every conversation creation.
 *
 * Updates are prefix-only: a caller may append the Agent ID to the already durable
 * Build ID, but can never reorder, remove, or replace prior scope.  The temporary
 * file and atomic rename prevent an early process exit from leaving partial JSON.
 */
export function updateProviderConversationManifest(
  conversationIds: readonly string[],
  env: NodeJS.ProcessEnv = process.env,
): void {
  validateIds(conversationIds);
  const configuredPath = env[PROVIDER_CONVERSATION_MANIFEST_ENV]?.trim() ?? "";
  if (!configuredPath || !path.isAbsolute(configuredPath))
    throw new Error(
      `${PROVIDER_CONVERSATION_MANIFEST_ENV} must be an absolute path`,
    );
  const suiteOut = env.DISCO_RELIABILITY_SUITE_OUT?.trim() ?? "";
  if (!suiteOut || !path.isAbsolute(suiteOut))
    throw new Error("DISCO_RELIABILITY_SUITE_OUT must be an absolute runner path");
  const resolvedSuiteOut = path.resolve(suiteOut);
  const resolvedManifest = path.resolve(configuredPath);
  if (path.dirname(resolvedManifest) !== resolvedSuiteOut)
    throw new Error("provider manifest must be suite-private");

  const existing = openExistingManifest(resolvedManifest);
  if (existing !== null) {
    try {
      if (
        existing.manifest.conversation_ids.length > conversationIds.length ||
        !existing.manifest.conversation_ids.every(
          (cid, index) => cid === conversationIds[index],
        )
      )
        throw new Error(
          "provider manifest updates must preserve the existing ID prefix",
        );
      if (existing.manifest.conversation_ids.length === conversationIds.length) {
        fs.fchmodSync(existing.descriptor, 0o600);
        fs.fsyncSync(existing.descriptor);
        return;
      }
    } finally {
      fs.closeSync(existing.descriptor);
    }
  }

  const payload = `${JSON.stringify(
    {
      schema_version: 1,
      conversation_ids: [...conversationIds],
    } satisfies Manifest,
    null,
    2,
  )}\n`;
  const temporary = `${resolvedManifest}.${process.pid}.${randomUUID()}.tmp`;
  let descriptor: number | undefined;
  try {
    descriptor = fs.openSync(temporary, "wx", 0o600);
    fs.writeFileSync(descriptor, payload, { encoding: "utf-8" });
    fs.fchmodSync(descriptor, 0o600);
    fs.fsyncSync(descriptor);
    fs.closeSync(descriptor);
    descriptor = undefined;
    fs.renameSync(temporary, resolvedManifest);
    syncDirectory(resolvedSuiteOut);
  } finally {
    if (descriptor !== undefined) fs.closeSync(descriptor);
    fs.rmSync(temporary, { force: true });
  }
}
