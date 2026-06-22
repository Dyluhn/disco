/**
 * The wire shape of a Settings "test" probe result (T4.x). Mirrors the backend
 * ProbeResult (app-server config/dtos.py + agent-server routes/probes.py). A
 * green (`ok: true`) means the provider REALLY answered — never a fake green;
 * an expected failure comes back `ok: false` with an honest `status` + `detail`.
 */
export interface ProbeResult {
  ok: boolean;
  /** Machine-readable: ok | unauthorized | unreachable | misconfigured |
   * disabled | procedural-fallback | bundled | error. */
  status: string;
  detail?: string;
  provider?: string | null;
  tool_count?: number | null;
  byte_count?: number | null;
  procedural?: boolean | null;
}
