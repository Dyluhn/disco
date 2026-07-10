// auth-kit 1.0.0 — the seam. createAuthApp() OWNS /auth/login, /auth/logout,
// /auth/session, and the app-wide session guard: protection is opt-OUT via
// config.publicAllowlist, never opt-in per route (spec §3 seam-ownership).
// Every other path is decided guard-first — the 401 happens BEFORE the
// wrapped handler is ever invoked.

import { createSession, destroySession, getSession, verifyPassword } from "./auth.js";

const SESSION_COOKIE = "tc_session";

function parseCookies(header) {
  const out = {};
  if (!header) return out;
  for (const part of header.split(";")) {
    const idx = part.indexOf("=");
    if (idx === -1) continue;
    const key = part.slice(0, idx).trim();
    if (!key) continue;
    const value = part.slice(idx + 1).trim();
    try {
      out[key] = decodeURIComponent(value);
    } catch {
      out[key] = value;
    }
  }
  return out;
}

function sessionCookieHeader(token, ttlHours) {
  const maxAge = Math.max(0, Math.round(Number(ttlHours) * 3600));
  return `${SESSION_COOKIE}=${token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=${maxAge}`;
}

function clearCookieHeader() {
  return `${SESSION_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0`;
}

function sendJson(res, status, body) {
  const data = JSON.stringify(body);
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(data),
  });
  res.end(data);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

// Exact match, or prefix match when the allowlist entry ends with "*".
function isPublic(pathname, allowlist) {
  for (const entry of allowlist || []) {
    if (entry.endsWith("*")) {
      if (pathname.startsWith(entry.slice(0, -1))) return true;
    } else if (pathname === entry) {
      return true;
    }
  }
  return false;
}

export function createAuthApp({ db, config, handler }) {
  return async function authApp(req, res) {
    let pathname;
    try {
      pathname = new URL(req.url, "http://localhost").pathname;
    } catch {
      pathname = req.url || "/";
    }
    const cookies = parseCookies(req.headers.cookie);
    const token = cookies[SESSION_COOKIE];

    if (req.method === "POST" && pathname === "/auth/login") {
      let body = {};
      try {
        body = JSON.parse((await readBody(req)) || "{}");
      } catch {
        sendJson(res, 401, { error: "invalid credentials" });
        return;
      }
      const { email, password } = body || {};
      const row =
        email != null
          ? db.prepare("SELECT id, email, password_hash FROM users WHERE email = ?").get(email)
          : null;
      if (!row || !password || !verifyPassword(password, row.password_hash)) {
        sendJson(res, 401, { error: "invalid credentials" });
        return;
      }
      const sessionToken = createSession(db, row.id, config.sessionTtlHours);
      res.setHeader("Set-Cookie", sessionCookieHeader(sessionToken, config.sessionTtlHours));
      sendJson(res, 200, { email: row.email });
      return;
    }

    if (req.method === "POST" && pathname === "/auth/logout") {
      if (token) destroySession(db, token);
      res.setHeader("Set-Cookie", clearCookieHeader());
      sendJson(res, 200, { ok: true });
      return;
    }

    if (req.method === "GET" && pathname === "/auth/session") {
      const session = getSession(db, token);
      if (!session) {
        sendJson(res, 401, { error: "unauthorized" });
        return;
      }
      sendJson(res, 200, { email: session.email });
      return;
    }

    if (isPublic(pathname, config.publicAllowlist)) {
      handler(req, res);
      return;
    }

    // Guard-first: the 401 is decided BEFORE handler() is ever called.
    const session = getSession(db, token);
    if (!session) {
      sendJson(res, 401, { error: "unauthorized" });
      return;
    }
    handler(req, res);
  };
}
