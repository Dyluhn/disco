// rbac-kit 1.0.0 — the role seam. createRbacApp() wraps an app handler and,
// for routes named in config.roleRoutes, requires the CURRENT USER to hold the
// mapped role (looked up server-side from user_roles) before the handler runs
// — else 403. It is designed to mount INSIDE auth-kit's chain:
//
//   createAuthApp({ db, config: authConfig,
//     handler: createRbacApp({ db, config: rbacConfig, handler: appHandler }) })
//
// auth-kit guarantees a valid session (401 otherwise); rbac-kit adds the role
// check on top. The role is ALWAYS read from the database via the session's
// userId — never from a request header/cookie/body — so a client that forges a
// role claim gains nothing. The seam canonicalizes the path itself (even though
// auth-kit already did) so it is safe even if mis-mounted without auth-kit.

import { getSession } from "../../auth-kit/core/auth.js";
import { hasRole } from "./roles.js";

const SESSION_COOKIE = "tc_session";

function sendJson(res, status, body) {
  const data = JSON.stringify(body);
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(data),
  });
  res.end(data);
}

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

// Same canonicalization auth-kit's guard uses (mirrored deliberately, so rbac is
// self-contained and never trusts an upstream to have normalized the target):
// strip query/fragment, reject encoded slash/backslash, collapse //, \\, and
// resolve . / .. so a crafted target cannot dodge a roleRoutes match.
function canonicalPath(rawUrl) {
  const raw = typeof rawUrl === "string" && rawUrl.length ? rawUrl : "/";
  let end = raw.length;
  const q = raw.indexOf("?");
  if (q !== -1) end = q;
  const h = raw.indexOf("#");
  if (h !== -1 && h < end) end = h;
  const pathPart = raw.slice(0, end);
  if (/%2f|%5c/i.test(pathPart)) return null;
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

// First matching roleRoutes entry wins. Keys are exact paths or "prefix*"
// wildcards (same grammar as auth-kit's publicAllowlist). Value is the role
// name required for that route.
function requiredRoleFor(pathname, roleRoutes) {
  if (!roleRoutes || typeof roleRoutes !== "object") return null;
  for (const [pattern, role] of Object.entries(roleRoutes)) {
    if (typeof pattern !== "string" || typeof role !== "string") continue;
    if (pattern.endsWith("*")) {
      if (pathname.startsWith(pattern.slice(0, -1))) return role;
    } else if (pathname === pattern) {
      return role;
    }
  }
  return null;
}

export function createRbacApp({ db, config, handler }) {
  const roleRoutes = (config && config.roleRoutes) || {};
  return function rbacApp(req, res) {
    try {
      const pathname = canonicalPath(req.url);
      if (pathname === null) {
        sendJson(res, 400, { error: "bad request" });
        return;
      }
      const requiredRole = requiredRoleFor(pathname, roleRoutes);
      if (requiredRole === null) {
        handler(req, res); // no role gate on this route
        return;
      }
      const cookies = parseCookies(req.headers.cookie);
      const session = getSession(db, cookies[SESSION_COOKIE]);
      if (!session) {
        // Fail closed: rbac is normally mounted behind auth-kit (so this is
        // already a 401 upstream), but if it is not, an unauthenticated request
        // to a role-gated route is still denied here.
        sendJson(res, 401, { error: "unauthorized" });
        return;
      }
      if (!hasRole(db, session.userId, requiredRole)) {
        sendJson(res, 403, { error: "forbidden", requiredRole });
        return;
      }
      handler(req, res);
    } catch {
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
