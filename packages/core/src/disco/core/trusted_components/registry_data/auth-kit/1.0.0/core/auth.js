// auth-kit 1.0.0 — password hashing, session lifecycle, dev-user seeding.
//
// Plain ESM, Node >= 22, node:crypto only. No npm dependencies (runtime
// contract). Every function here is pure db-in/db-out — the HTTP seam lives
// in middleware.js, which is the ONLY file that owns routing decisions.

import { createHash, randomBytes, scryptSync, timingSafeEqual } from "node:crypto";

const SCRYPT_KEYLEN = 64;

// A well-formed but never-matching hash. Login runs a real scrypt against this
// when the email is unknown, so "no such user" costs the same wall-clock as a
// wrong password — no user-enumeration timing oracle. Its expected length is
// SCRYPT_KEYLEN, so the decoy scrypt does the SAME work as a real verify.
const DECOY_HASH = `scrypt:${"0".repeat(32)}:${"0".repeat(SCRYPT_KEYLEN * 2)}`;

// Session tokens are stored HASHED (sha256) at rest: a read of the sessions
// table (SQLi elsewhere, a leaked backup) yields hashes, not live bearer
// tokens. The raw token lives only in the client cookie.
function hashToken(token) {
  return createHash("sha256").update(String(token)).digest("hex");
}

// "scrypt:<salt-hex>:<hash-hex>" — a per-user random 16-byte salt, so two
// users with the same password never share a hash.
export function hashPassword(password) {
  const salt = randomBytes(16);
  const derived = scryptSync(password, salt, SCRYPT_KEYLEN);
  return `scrypt:${salt.toString("hex")}:${derived.toString("hex")}`;
}

// Constant-time compare against a "scrypt:<salt>:<hash>" record. Any
// malformed `stored` value (wrong format, bad hex) is a clean `false`, never
// a thrown error — a corrupt row must never crash the auth seam. A non-string
// `password` is also a clean `false` (never a scryptSync ERR_INVALID_ARG_TYPE).
export function verifyPassword(password, stored) {
  if (typeof stored !== "string" || typeof password !== "string") return false;
  const parts = stored.split(":");
  if (parts.length !== 3 || parts[0] !== "scrypt") return false;
  const [, saltHex, hashHex] = parts;
  let salt;
  let expected;
  try {
    salt = Buffer.from(saltHex, "hex");
    expected = Buffer.from(hashHex, "hex");
  } catch {
    return false;
  }
  if (salt.length === 0 || expected.length === 0) return false;
  const actual = scryptSync(password, salt, expected.length);
  if (actual.length !== expected.length) return false;
  return timingSafeEqual(actual, expected);
}

// The one credential-check entry point the seam uses. Type-guards BOTH inputs
// so a JSON object/array can never reach node:sqlite's bind (which treats an
// object as NAMED params and throws) or scryptSync (ERR_INVALID_ARG_TYPE).
// Always performs exactly one scrypt (real hash when the user exists, decoy
// when it does not) so presence/absence of a user is not timing-observable.
// Returns {id, email} on success, or null.
export function authenticateUser(db, email, password) {
  if (typeof email !== "string" || typeof password !== "string") {
    verifyPassword("decoy", DECOY_HASH); // flatten timing even for malformed input
    return null;
  }
  const row = db.prepare("SELECT id, email, password_hash FROM users WHERE email = ?").get(email);
  const stored = row ? row.password_hash : DECOY_HASH;
  const ok = verifyPassword(password, stored);
  return ok && row ? { id: row.id, email: row.email } : null;
}

export function createUser(db, email, password) {
  const passwordHash = hashPassword(password);
  const createdAt = new Date().toISOString();
  const info = db
    .prepare("INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)")
    .run(email, passwordHash, createdAt);
  return { id: Number(info.lastInsertRowid), email };
}

export function createSession(db, userId, ttlHours) {
  const token = randomBytes(32).toString("hex");
  const expiresAt = new Date(Date.now() + ttlHours * 3600 * 1000).toISOString();
  db.prepare("INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)").run(
    hashToken(token),
    userId,
    expiresAt,
  );
  return token;
}

// {userId, email} for a live session, or null. Expired sessions are BOTH
// reported as null AND deleted here (lazy sweep — no separate cron needed).
export function getSession(db, token) {
  if (!token || typeof token !== "string") return null;
  const tokenHash = hashToken(token);
  const row = db
    .prepare(
      `SELECT sessions.user_id AS userId, sessions.expires_at AS expiresAt, users.email AS email
         FROM sessions
         JOIN users ON users.id = sessions.user_id
        WHERE sessions.token = ?`,
    )
    .get(tokenHash);
  if (!row) return null;
  if (new Date(row.expiresAt).getTime() <= Date.now()) {
    db.prepare("DELETE FROM sessions WHERE token = ?").run(tokenHash);
    return null;
  }
  return { userId: row.userId, email: row.email };
}

export function destroySession(db, token) {
  if (!token || typeof token !== "string") return;
  db.prepare("DELETE FROM sessions WHERE token = ?").run(hashToken(token));
}

// Idempotent: only runs when config.devSeedUser is non-null, and only when
// no user with that email already exists.
export function seedDevUser(db, config) {
  const seed = config && config.devSeedUser;
  if (!seed) return;
  const existing = db.prepare("SELECT id FROM users WHERE email = ?").get(seed.email);
  if (existing) return;
  createUser(db, seed.email, seed.password);
}
