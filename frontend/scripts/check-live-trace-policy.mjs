import { existsSync, mkdtempSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const output = mkdtempSync(join(tmpdir(), "disco-live-trace-policy-"));
const marker = join(output, "browser-reached-failure");

function findTraceArchives(path) {
  const found = [];
  for (const entry of readdirSync(path, { withFileTypes: true })) {
    const child = join(path, entry.name);
    if (entry.isDirectory()) found.push(...findTraceArchives(child));
    else if (entry.name === "trace.zip") found.push(child);
  }
  return found;
}

try {
  const result = spawnSync(
    join(root, "node_modules", ".bin", "playwright"),
    ["test", "--config", "playwright.trace-policy.config.ts"],
    {
      cwd: root,
      encoding: "utf8",
      env: {
        HOME: process.env.HOME,
        PATH: process.env.PATH,
        XDG_CACHE_HOME: process.env.XDG_CACHE_HOME,
        CI: "1",
        DISCO_TRACE_POLICY_OUTPUT: output,
        DISCO_TRACE_POLICY_MARKER: marker,
      },
    },
  );
  const traces = findTraceArchives(output);
  const expectedFailureReported = /1 failed/.test(result.stdout ?? "");
  if (
    result.status !== 1 ||
    !expectedFailureReported ||
    !existsSync(marker) ||
    traces.length !== 0
  ) {
    process.stderr.write(result.stdout ?? "");
    process.stderr.write(result.stderr ?? "");
    throw new Error(
      `live trace policy failed: exit=${result.status} expected_failure=${expectedFailureReported} browser_marker=${existsSync(marker)} traces=${traces.length}`,
    );
  }
  process.stdout.write(
    "live authenticated failure trace policy: PASS (no trace.zip)\n",
  );
} finally {
  rmSync(output, { recursive: true, force: true });
}
