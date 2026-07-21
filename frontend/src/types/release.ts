/**
 * Release-capability types — the wire shape of `GET /api/projects/{id}/release`
 * (WO-7). Mirrors the agent-server `ReleaseResponse` model field-for-field so the
 * UI parses ONE stable shape for every assessment (`candidate` / `needs_review` /
 * `not_web`): non-applicable fields are `null`/empty rather than absent.
 *
 * Secret hygiene (inherited, not re-invented): a `required_env` entry carries the
 * env-var NAME + metadata ONLY — there is deliberately NO `value` field, so a
 * runtime secret can never ride this response into the UI. The host injects the
 * concrete values out-of-band; the browser only ever learns the names.
 */

/** The detector's verdict on whether/how a workspace can be released. Mirrors the
 * Python `ReleaseAssessment` enum string values EXACTLY. All SIX values exist for
 * forward-compatibility even though Track 1 only ever emits the first three
 * (`not_web` / `candidate` / `needs_review`); the verification-lifecycle values
 * (`verifying` / `verified` / `failed`) are defined up front so the union — and
 * every persisted assessment referencing it — is stable when a later track wires
 * the verification loop (no enum bump, no migration). */
export type ReleaseAssessmentState =
  | "not_web"
  | "candidate"
  | "needs_review"
  | "verifying"
  | "verified"
  | "failed";

/** When an env var is consumed — mirrors the Python `EnvScope` enum: at `build`
 * time or at `runtime`. */
export type ReleaseEnvScope = "runtime" | "build";

/** One reason a release is not (fully) ready, as flat DATA. `field` names a
 * release-contract field an owner must declare (a repairable diagnostic); `path`
 * names a workspace/overlay path a finding is about. Both optional/nullable so
 * detection diagnostics, validation blockers, and overlay-collision reports share
 * ONE shape. */
export interface ReleaseBlocker {
  code: string;
  message: string;
  field?: string | null;
  path?: string | null;
}

/** A declared env var the release reads — its NAME + metadata, NEVER a value. */
export interface ReleaseEnv {
  name: string;
  scope: ReleaseEnvScope;
  required: boolean;
  secret: boolean;
}

/** The single public entrypoint of a detected release. `port` is the env-var NAME
 * the ingress binds (the `$PORT` contract) — NOT a literal port number; the host
 * owns the concrete port at deploy time. */
export interface ReleaseIngress {
  service: string;
  port: string;
  health_path: string | null;
}

/** The `/release` verdict. The key set is IDENTICAL for every assessment —
 * non-applicable fields are `null`/empty rather than absent, so a consumer parses
 * one stable shape. */
export interface ReleaseResponse {
  assessment: ReleaseAssessmentState;
  reasons: string[];
  blockers: ReleaseBlocker[];
  required_env: ReleaseEnv[];
  command: string;
  ingress: ReleaseIngress | null;
  self_host: boolean;
  spec_digest: string | null;
  // Nullable to mirror the agent-server `ReleaseResponse` (`int | None` / `str | None`):
  // an UNSNAPSHOTTED workspace has never committed a version, so it can name no
  // concrete committed source — `version_seq` and `tree_digest` come back `null`. A
  // consumer that wants to pin a source-bound download must narrow BOTH to non-null
  // first (see `downloadProject`'s `DownloadBinding`), which is the guard that keeps
  // an unbound release from ever riding a bound download URL.
  version_seq: number | null;
  tree_digest: string | null;
}
