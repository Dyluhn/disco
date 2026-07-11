# WO-A4 scoped host credential threat model

This document covers only the WO-A4 security half: host-side token format,
minting, rotation, and service/origin scopes. Quotas, accounting, rate limits,
and `ai.chat` are separate work.

## Security boundary and format

The sandbox receives an opaque bearer with wire format
`a4v1.<22-char selector>.<43-char verifier>`. The selector only locates a row;
the 256-bit verifier carries the authentication strength. SQLite stores the
selector, explicit format version, trusted owner/conversation/audience, exact
service and origin sets, lifecycle metadata, and only SHA-256(verifier). New
mints are always v1. A schema migration marks pre-existing rows v0 so an active,
persisted `a2v0` token survives upgrade; changing the prefix of either version
does not work because the parsed version must equal the stored version.

## Isolation and non-amplification claims

- **App A cannot act as app B.** Audience is host-minted record data, not a
  caller claim. The bus constructs its service context from that record. A
  rotation finishes only within the candidate's exact
  `(owner, conversation, audience)` tuple, so it cannot revoke or replace a
  same-named app belonging to another owner.
- **A token cannot broaden its service scope.** The requested route is decoded
  and canonicalized independently and must be an exact member of the persisted
  service set. Stored service names are revalidated on read; malformed,
  duplicate, empty, or non-canonical scope data fails closed.
- **A token cannot broaden its origin scope.** Origins are canonicalized at mint
  and revalidated when read. The trusted context receives only that immutable
  exact set; credentials cannot add a path, credentials, query, fragment, or a
  non-loopback plaintext origin.
- **Database theft does not reveal a bearer.** The verifier is returned once and
  never persisted or logged. An offline attacker with the database still must
  guess a 256-bit verifier.

## Probing, rotation, and rollback

Unknown selectors and wrong verifiers take the same SHA-256 plus
`compare_digest` authentication path. The dummy digest prevents the selector
existence branch from skipping constant-time digest comparison. This does not
claim whole-request timing equality with SQLite cache effects; it removes the
secret-dependent verifier comparison and the obvious unknown-selector oracle.

Rotation is candidate-then-finish. `rotate` allocates the next generation under
`BEGIN IMMEDIATE`, using the maximum generation for the exact
`(owner, conversation, audience)` tuple, so concurrent hosts cannot reuse or
decrease a generation. Existing credentials stay active while the candidate is
delivered and probed. Only `finish_rotation` revokes older credentials, in one
transaction, after confirming that candidate is active for the target app. If
delivery or probing fails, callers revoke only the candidate; the old credential
remains usable. If finish fails, its transaction rolls back and no older token is
partially revoked. Concurrent finishes serialize: one candidate wins and the
other fails because it is no longer active.

## Residual risks

Bearers remain replayable until expiry/revocation, and v0 migration support
extends the lifetime of already-persisted active A2 tokens. Compromise of the
running sandbox exposes its own bearer and therefore its already-approved exact
capabilities. Transport confidentiality and sandbox-to-host reachability remain
deployment responsibilities. Quotas and abuse-rate controls are intentionally
not supplied by this security slice.
