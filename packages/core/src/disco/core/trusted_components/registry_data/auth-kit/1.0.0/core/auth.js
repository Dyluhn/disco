// auth-kit 1.0.0 — password hashing, session lifecycle, dev-user seeding.
//
// Plain ESM, Node >= 22, node:crypto only. No npm dependencies (runtime
// contract). Every function here is pure db-in/db-out — the HTTP seam lives
// in middleware.js, which is the ONLY file that owns routing decisions.

import { randomBytes, scryptSync, timingSafeEqual } from "node:crypto";

const SCRYPT_KEYLEN = 64;

// "scrypt:<salt-hex>:<hash-hex>" — a per-user random 16-byte salt, so two
// users with the same password never share a hash.
export function hashPassword(password) {
  const salt = randomBytes(16);
  const derived = scryptSync(password, salt, SCRYPT_KEYLEN);
  return `scrypt:${salt.toString("hex")}:${derived.toString("hex")}`;
}

// Constant-time compare against a "scrypt:<salt>:<hash>" record. Any
// malformed `stored` value (wrong format, bad hex) is a clean `false`, never
// a thrown error — a corrupt row must never crash the auth seam.
export function verifyPassword(password, stored) {
  if (typeof stored !== "string") return false;
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
    token,
    userId,
    expiresAt,
  );
  return token;
}

// {userId, email} for a live session, or null. Expired sessions are BOTH
// reported as null AND deleted here (lazy sweep — no separate cron needed).
export function getSession(db, token) {
  if (!token) return null;
  const row = db
    .prepare(
      `SELECT sessions.user_id AS userId, sessions.expires_at AS expiresAt, users.email AS email
         FROM sessions
         JOIN users ON users.id = sessions.user_id
        WHERE sessions.token = ?`,
    )
    .get(token);
  if (!row) return null;
  if (new Date(row.expiresAt).getTime() <= Date.now()) {
    destroySession(db, token);
    return null;
  }
  return { userId: row.userId, email: row.email };
}

export function destroySession(db, token) {
  db.prepare("DELETE FROM sessions WHERE token = ?").run(token);
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
