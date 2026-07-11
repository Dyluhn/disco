// auth-kit 1.0.0 — the seam. createAuthApp() OWNS /auth/login, /auth/logout,
// /auth/session, and the app-wide session guard: protection is opt-OUT via
// config.publicAllowlist, never opt-in per route (spec §3 seam-ownership).
// Every other path is decided guard-first — the 401 happens BEFORE the
// wrapped handler is ever invoked.
//
// The guard canonicalizes the request path ITSELF and rewrites req.url to that
// canonical form, so the path the guard authorizes is byte-identical to the one
// the wrapped handler routes on — a protocol-relative "//admin", a "\\", a
// "/../" or an encoded-slash target can never read as public here and resolve
// to a protected route downstream. The whole seam runs inside a fail-closed
// try/catch: no malformed request can throw the seam out and kill the process.

import { authenticateUser, createSession, destroySession, getSession } from "./auth.js";

const SESSION_COOKIE = "tc_session";
const DEFAULT_MAX_BODY_BYTES = 64 * 1024;
const DEFAULT_RATE_WINDOW_MS = 60_000;
const DEFAULT_RATE_MAX = 10;
const RATE_MAX_TRACKED_IPS = 10_000;

class BodyTooLarge extends Error {}

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

function cookieHeader(token, ttlHours, secure) {
  const maxAge = Math.max(0, Math.round(Number(ttlHours) * 3600));
  const base = `${SESSION_COOKIE}=${token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=${maxAge}`;
  return secure ? `${base}; Secure` : base;
}

function clearCookieHeader(secure) {
  const base = `${SESSION_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0`;
  return secure ? `${base}; Secure` : base;
}

function sendJson(res, status, body) {
  const data = JSON.stringify(body);
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(data),
  });
  res.end(data);
}

// The body was capped mid-stream, so the socket still has an undelivered
// remainder the client declared in Content-Length. Answering keep-alive would
// leave the connection stuck (Node waits for the rest before the next request)
// — a cheap socket-exhaustion primitive. Send 413 with Connection: close and
// tear the socket down once the response has flushed.
function refuseTooLarge(req, res) {
  if (res.headersSent) {
    try {
      res.destroy();
    } catch {
      /* nothing left to do */
    }
    return;
  }
  const data = JSON.stringify({ error: "payload too large" });
  res.writeHead(413, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(data),
    Connection: "close",
  });
  res.end(data);
  res.once("finish", () => {
    try {
      req.socket.destroy();
    } catch {
      /* already gone */
    }
  });
}

// Read the body with a hard byte cap — an unbounded Buffer.concat is a trivial
// memory-exhaustion DoS on a route (login) that is public by necessity.
function readBody(req, maxBytes) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let total = 0;
    let done = false;
    req.on("data", (chunk) => {
      if (done) return;
      total += chunk.length;
      if (total > maxBytes) {
        done = true;
        req.pause(); // stop the flow; let the handler answer 413 without a torn socket
        reject(new BodyTooLarge());
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => {
      if (done) return;
      done = true;
      resolve(Buffer.concat(chunks).toString("utf8"));
    });
    req.on("error", (err) => {
      if (done) return;
      done = true;
      reject(err);
    });
  });
}

// Split "path?query#frag" into { pathPart, suffix } WITHOUT new URL() — the
// WHATWG URL parser reads a leading "//" as an authority, collapsing "//admin"
// to pathname "/". Manual slicing keeps the real path characters.
function splitTarget(rawUrl) {
  const raw = typeof rawUrl === "string" && rawUrl.length ? rawUrl : "/";
  let end = raw.length;
  const q = raw.indexOf("?");
  if (q !== -1) end = q;
  const h = raw.indexOf("#");
  if (h !== -1 && h < end) end = h;
  return { pathPart: raw.slice(0, end), suffix: raw.slice(end) };
}

// One canonical form the guard AND the wrapped handler both see: forward slashes,
// no empty / "." / ".." segments, exactly one leading slash. Returns null (=>
// fail closed with 400) for a target carrying an encoded slash, backslash, or
// dot (%2f/%5c/%2e). WHATWG URL parsing (which a downstream router uses) decodes
// %2e as a dot-segment and keeps %2f literal, so an un-decoded encoded form
// would let the guard and the router disagree on the path — e.g. "/pub/%2e%2e/
// admin" reads as public here but resolves to /admin downstream. Rejecting the
// encodings outright closes that desync without guessing the router's decode.
function canonicalize(pathPart) {
  if (/%2e|%2f|%5c/i.test(pathPart)) return null;
  const normalized = pathPart.replace(/\\/g, "/");
  const out = [];
  for (const seg of normalized.split("/")) {
    if (seg === "" || seg === ".") continue;
    if (seg === "..") {
      out.pop();
      continue;
    }
    out.push(seg);
  }
  return `/${out.join("/")}`;
}

// Exact match, or prefix match when the allowlist entry ends with "*".
function isPublic(pathname, allowlist) {
  for (const entry of allowlist || []) {
    if (typeof entry !== "string") continue;
    if (entry.endsWith("*")) {
      if (pathname.startsWith(entry.slice(0, -1))) return true;
    } else if (pathname === entry) {
      return true;
    }
  }
  return false;
}

// The rate-limit key. X-Forwarded-For is CLIENT-CONTROLLED and only meaningful
// behind a trusted reverse proxy, so it is honored ONLY when config.trustProxy
// is set — otherwise a caller could mint a fresh limiter bucket per request by
// spoofing the header and defeat the throttle entirely. Default: the real peer.
function clientIp(req, config) {
  if (config && config.trustProxy) {
    const xff = req.headers["x-forwarded-for"];
    if (typeof xff === "string" && xff.length) {
      const first = xff.split(",")[0].trim();
      if (first) return first;
    }
  }
  return (req.socket && req.socket.remoteAddress) || "unknown";
}

// TLS-aware: Secure cookies when the connection is actually HTTPS. A direct TLS
// socket always counts; x-forwarded-proto is client-controlled so it is trusted
// ONLY behind config.trustProxy. config.cookieSecure (boolean) forces either way.
function isSecureRequest(req, config) {
  if (config && typeof config.cookieSecure === "boolean") return config.cookieSecure;
  if (req.socket && req.socket.encrypted) return true;
  if (config && config.trustProxy) {
    const xfp = req.headers["x-forwarded-proto"];
    if (typeof xfp === "string" && xfp.split(",")[0].trim().toLowerCase() === "https") return true;
  }
  return false;
}

// Best-effort per-process sliding-window limiter. Bounds memory by dropping the
// tracking table when distinct-IP count goes pathological (each IP already has
// its own budget, so a flush never grants an attacker more than a fresh window).
function makeLoginLimiter(config) {
  const cfg = (config && config.loginRateLimit) || {};
  const windowMs = Number.isFinite(cfg.windowMs) ? cfg.windowMs : DEFAULT_RATE_WINDOW_MS;
  const max = Number.isFinite(cfg.max) ? cfg.max : DEFAULT_RATE_MAX;
  const hits = new Map();
  return function allow(ip, now) {
    if (max <= 0) return true;
    let arr = hits.get(ip);
    if (!arr) {
      if (hits.size >= RATE_MAX_TRACKED_IPS) hits.clear();
      arr = [];
      hits.set(ip, arr);
    }
    const cutoff = now - windowMs;
    while (arr.length && arr[0] <= cutoff) arr.shift();
    if (arr.length === 0) hits.delete(ip); // reclaim, then re-add below if allowed
    if (arr.length >= max) {
      hits.set(ip, arr);
      return false;
    }
    arr.push(now);
    hits.set(ip, arr);
    return true;
  };
}

export function createAuthApp({ db, config, handler }) {
  const maxBodyBytes = Number.isFinite(config && config.maxBodyBytes)
    ? config.maxBodyBytes
    : DEFAULT_MAX_BODY_BYTES;
  const loginAllow = makeLoginLimiter(config);

  return async function authApp(req, res) {
    try {
      const { pathPart, suffix } = splitTarget(req.url);
      const pathname = canonicalize(pathPart);
      if (pathname === null) {
        sendJson(res, 400, { error: "bad request" });
        return;
      }
      // Rewrite so the wrapped handler routes on the SAME path the guard decided.
      req.url = pathname + suffix;

      const secure = isSecureRequest(req, config);
      const cookies = parseCookies(req.headers.cookie);
      const token = cookies[SESSION_COOKIE];

      if (req.method === "POST" && pathname === "/auth/login") {
        if (!loginAllow(clientIp(req, config), Date.now())) {
          sendJson(res, 429, { error: "too many attempts" });
          return;
        }
        let body = {};
        try {
          body = JSON.parse((await readBody(req, maxBodyBytes)) || "{}");
        } catch (err) {
          if (err instanceof BodyTooLarge) {
            refuseTooLarge(req, res);
            return;
          }
          sendJson(res, 401, { error: "invalid credentials" });
          return;
        }
        const { email, password } = body || {};
        // authenticateUser type-checks email/password (never lets a non-string
        // reach node:sqlite bind or scryptSync) and burns one scrypt on every
        // path, so a missing user is not a timing oracle.
        const user = authenticateUser(db, email, password);
        if (!user) {
          sendJson(res, 401, { error: "invalid credentials" });
          return;
        }
        const sessionToken = createSession(db, user.id, config.sessionTtlHours);
        res.setHeader("Set-Cookie", cookieHeader(sessionToken, config.sessionTtlHours, secure));
        sendJson(res, 200, { email: user.email });
        return;
      }

      if (req.method === "POST" && pathname === "/auth/logout") {
        if (token) destroySession(db, token);
        res.setHeader("Set-Cookie", clearCookieHeader(secure));
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
    } catch {
      // Fail closed: any throw inside the seam becomes a 500, never a crashed
      // process. If the response was already started, sever it rather than
      // double-writing headers.
      if (!res.headersSent) {
        try {
          sendJson(res, 500, { error: "internal error" });
        } catch {
          try {
            res.destroy();
          } catch {
            /* nothing left to do */
          }
        }
      } else {
        try {
          res.destroy();
        } catch {
          /* nothing left to do */
        }
      }
    }
  };
}
