"""Generated React workspace for policy-enabled records apps."""

from __future__ import annotations

from ..spec import AppSpec, Entity
from .naming import _records_table_name


def _field_payload(entity: Entity) -> list[dict[str, object]]:
    return [
        {
            "name": field.name,
            "label": field.label or field.name.replace("_", " ").title(),
            "type": field.type,
            "required": field.required,
            "reference": field.references,
        }
        for field in entity.fields
    ]


def emit_records_policy_app_tsx(app: AppSpec, names: dict[tuple[str, str], str]) -> str:
    """App shell that retains authored sections and mounts the trusted workspace."""
    from ..generator import _comp_name, _iter_sections

    imports: list[str] = []
    renders: list[str] = []
    for page, section in _iter_sections(app):
        comp = _comp_name(names, page, section)
        imports.append(f'import {comp} from "./components/{comp}";')
        renders.append(f"      <{comp} />")
    return (
        "/* Auto-generated app shell — regenerated from .disco/appspec.json. */\n"
        + "\n".join(imports)
        + '\nimport RecordsWorkspace from "./components/RecordsWorkspace";\n\n'
        "export default function App() {\n"
        "  return (\n"
        '    <div className="app-main">\n' + "\n".join(renders) + "\n      <RecordsWorkspace />\n"
        "    </div>\n"
        "  );\n"
        "}\n"
    )


def _entity_payload(app: AppSpec) -> list[dict[str, object]]:
    from ..form_primitive import form_submission_entities_for

    policy_entities = tuple(entity for entity in app.entities if entity.record_policy is not None)
    form_ids = {
        entity.id
        for entity in form_submission_entities_for(app, reserved_entities=policy_entities)
    }
    payload: list[dict[str, object]] = []
    for entity in app.entities:
        if entity.id in form_ids:
            continue
        policy = entity.record_policy
        payload.append(
            {
                "id": entity.id,
                "name": entity.name,
                "table": _records_table_name(entity),
                "fields": _field_payload(entity),
                "createRoles": list(
                    policy.create_roles if policy is not None else entity.write_roles
                ),
                "ownerManaged": bool(policy and policy.owner_managed),
                "manageRoles": list(policy.manage_roles if policy is not None else ()),
                "lockRoles": list(policy.lock_roles if policy is not None else ()),
            }
        )
    return payload


def _emit_workspace_prelude(app: AppSpec) -> str:
    from ..generator import _ts

    return (
        'import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";\n\n'
        "type Session = { authenticated: boolean; user_id?: number; role?: string };\n"
        "type Row = Record<string, unknown> & { id: number; "
        "owner_user_id?: number; locked?: number };\n"
        "type FieldMeta = { name: string; label: string; type: string; required: boolean; "
        "reference: string | null };\n"
        "type EntityMeta = { id: string; name: string; table: string; fields: FieldMeta[]; "
        "createRoles: string[]; ownerManaged: boolean; manageRoles: string[]; "
        "lockRoles: string[] };\n\n"
        f"const ENTITIES: EntityMeta[] = {_ts(_entity_payload(app))};\n"
        f"const ROLES: string[] = {_ts(list(app.roles))};\n"
        f"const ROLE_ADMIN_ROLES: string[] = {_ts(list(app.role_admin_roles))};\n\n"
        "async function request(\n"
        "  path: string, init: RequestInit = {}\n"
        "): Promise<Record<string, unknown>> {\n"
        "  const headers = new Headers(init.headers);\n"
        '  if (init.body !== undefined) headers.set("Content-Type", "application/json");\n'
        '  const response = await fetch(path, { ...init, headers, credentials: "include" });\n'
        "  const data = await response.json().catch(() => ({})) as Record<string, unknown>;\n"
        '  if (!response.ok) throw new Error(typeof data.error === "string" ? data.error : '
        "`Request failed (${response.status})`);\n"
        "  return data;\n"
        "}\n\n"
        "function valueFor(field: FieldMeta, raw: string): string | number {\n"
        '  const numeric = field.reference !== null || ["int", "integer", "number"]\n'
        "    .includes(field.type.toLowerCase());\n"
        "  return numeric ? Number(raw) : raw;\n"
        "}\n\n"
    )


def _emit_entity_panel_logic_tsx() -> str:
    return (
        "function EntityPanel(\n"
        "  { meta, session, reload }: "
        "{ meta: EntityMeta; session: Session; reload: number }\n"
        ") {\n"
        "  const [rows, setRows] = useState<Row[]>([]);\n"
        "  const [form, setForm] = useState<Record<string, string>>({});\n"
        "  const [editing, setEditing] = useState<number | null>(null);\n"
        '  const [message, setMessage] = useState("");\n'
        "  const load = useCallback(async () => {\n"
        "    try {\n"
        "      const data = await request(`/api/${meta.table}`);\n"
        "      setRows(Array.isArray(data[meta.table]) ? data[meta.table] as Row[] : []);\n"
        "    } catch (error) {\n"
        '      setMessage(error instanceof Error ? error.message : "Could not load records");\n'
        "    }\n"
        "  }, [meta.table]);\n"
        "  useEffect(() => { void load(); }, [load, reload]);\n"
        "  const canCreate = Boolean(\n"
        "    session.authenticated && session.role && meta.createRoles.includes(session.role)\n"
        "  );\n"
        "  const reset = () => { setForm({}); setEditing(null); };\n"
        "  const submit = async (event: FormEvent) => {\n"
        '    event.preventDefault(); setMessage("");\n'
        "    const body = Object.fromEntries(meta.fields.map((field) => [\n"
        '      field.name, valueFor(field, form[field.name] ?? "")\n'
        "]));\n"
        "    try {\n"
        "      const path = editing === null "
        "? `/api/${meta.table}` : `/api/${meta.table}/${editing}`;\n"
        "      await request(path, {\n"
        '        method: editing === null ? "POST" : "PATCH", body: JSON.stringify(body),\n'
        "      });\n"
        "      setMessage(editing === null ? `${meta.name} created.` : `${meta.name} updated.`);\n"
        "      reset(); await load();\n"
        "    } catch (error) {\n"
        '      setMessage(error instanceof Error ? error.message : "Request failed");\n'
        "    }\n"
        "  };\n"
        "  const beginEdit = (row: Row) => {\n"
        "    setEditing(row.id);\n"
        "    setForm(Object.fromEntries(meta.fields.map((field) => [\n"
        '      field.name, String(row[field.name] ?? "")\n'
        "])));\n"
        "  };\n"
        "  const mutate = async (path: string, method: string) => {\n"
        '    setMessage("");\n'
        "    try { await request(path, { method }); await load(); }\n"
        "    catch (error) {\n"
        '      setMessage(error instanceof Error ? error.message : "Request failed");\n'
        "    }\n"
        "  };\n"
    )


def _emit_entity_panel_view_tsx() -> str:
    return (
        "  return (\n"
        '    <section className="records-panel" data-record-entity={meta.id}>\n'
        '      <div className="records-panel-heading"><div>'
        '<p className="eyebrow">Persistent records</p>'
        "<h3>{meta.name}</h3></div><span>{rows.length} total</span></div>\n"
        "      {canCreate || editing !== null ? (\n"
        '        <form className="records-form" onSubmit={submit}>\n'
        "          {meta.fields.map((field) => (\n"
        "            <label key={field.name}>{field.label}\n"
        "              <input name={field.name} required={field.required}\n"
        '                value={form[field.name] ?? ""}\n'
        '                inputMode={field.reference !== null ? "numeric" : undefined}\n'
        "                onChange={(event) => setForm((current) => ({\n"
        "                  ...current, [field.name]: event.target.value\n"
        "                }))} />\n"
        "            </label>\n"
        "          ))}\n"
        '          <div className="records-actions"><button className="btn" type="submit">'
        '{editing === null ? `Add ${meta.name}` : "Save changes"}</button>\n'
        '          {editing !== null ? <button type="button" onClick={reset}>'
        "Cancel</button> : null}</div>\n"
        "        </form>\n"
        '      ) : <p className="records-hint">'
        "Sign in with an authorized role to add records.</p>}\n"
        '      {message ? <p className="records-message" role="status">{message}</p> : null}\n'
        '      <div className="records-list">\n'
        "        {rows.map((row) => {\n"
        '          const role = session.role ?? "";\n'
        "          const canManage = meta.manageRoles.includes(role) || (\n"
        "            meta.ownerManaged && row.owner_user_id === session.user_id\n"
        "          );\n"
        "          const canLock = meta.lockRoles.includes(role);\n"
        '          return <article className="record-card" key={row.id}>\n'
        '            <div className="record-card-meta"><strong>#{row.id}</strong>'
        '{row.locked === 1 ? <span className="locked-badge">Locked</span> : null}</div>\n'
        "            <dl>{meta.fields.map((field) => <div key={field.name}><dt>{field.label}</dt>"
        '<dd>{String(row[field.name] ?? "—")}</dd></div>)}</dl>\n'
        '            {canManage || canLock ? <div className="records-actions">\n'
        '              {canManage ? <button type="button" '
        "onClick={() => beginEdit(row)}>Edit</button> : null}\n"
        '              {canManage ? <button className="danger" type="button"\n'
        "                onClick={() => void mutate("
        '`/api/${meta.table}/${row.id}`, "DELETE")}>Delete</button> : null}\n'
        '              {canLock ? <button type="button"\n'
        "                onClick={() => void mutate(\n"
        "                  `/api/${meta.table}/${row.id}/${row.locked === 1 "
        '? "unlock" : "lock"}`, "POST"\n'
        '                )}>{row.locked === 1 ? "Unlock" : "Lock"}</button> : null}\n'
        "            </div> : null}\n"
        "          </article>;\n"
        "        })}\n"
        "      </div>\n"
        "    </section>\n"
        "  );\n"
        "}\n\n"
    )


def _emit_workspace_logic_tsx() -> str:
    return (
        "export default function RecordsWorkspace() {\n"
        "  const [session, setSession] = useState<Session>({ authenticated: false });\n"
        '  const [email, setEmail] = useState("");\n'
        '  const [password, setPassword] = useState("");\n'
        '  const [message, setMessage] = useState("");\n'
        "  const [reload, setReload] = useState(0);\n"
        "  const [users, setUsers] = useState<Row[]>([]);\n"
        "  const isRoleAdmin = Boolean(session.role && ROLE_ADMIN_ROLES.includes(session.role));\n"
        "  const refreshSession = useCallback(async () => {\n"
        '    const data = await request("/api/session");\n'
        "    setSession(data as Session);\n"
        "  }, []);\n"
        "  useEffect(() => { void refreshSession(); }, [refreshSession]);\n"
        "  useEffect(() => {\n"
        "    if (!isRoleAdmin) { setUsers([]); return; }\n"
        '    void request("/api/users").then((data) => {\n'
        "      setUsers(Array.isArray(data.users) ? data.users as Row[] : []);\n"
        "    });\n"
        "  }, [isRoleAdmin, reload]);\n"
        "  const login = async (event: FormEvent) => {\n"
        '    event.preventDefault(); setMessage("");\n'
        "    try {\n"
        '      await request("/api/login", {\n'
        '        method: "POST", body: JSON.stringify({ email, password })\n'
        "      });\n"
        '      await refreshSession(); setPassword(""); setReload((value) => value + 1);\n'
        "    } catch (error) {\n"
        '      setMessage(error instanceof Error ? error.message : "Login failed");\n'
        "    }\n"
        "  };\n"
        '  const logout = async () => { await request("/api/logout", { method: "POST" }); '
        "setSession({ authenticated: false }); setUsers([]); setReload((value) => value + 1); };\n"
        "  const roleOptions = useMemo(() => ROLES.map((role) => (\n"
        "    <option key={role} value={role}>{role}</option>\n"
        "  )), []);\n"
        "  const changeRole = async (userId: number, role: string) => {\n"
        "    try {\n"
        "      await request(`/api/users/${userId}/role`, {\n"
        '        method: "PATCH", body: JSON.stringify({ role })\n'
        "      });\n"
        '      setReload((value) => value + 1); setMessage("Role updated.");\n'
        "    } catch (error) {\n"
        '      setMessage(error instanceof Error ? error.message : "Role update failed");\n'
        "    }\n"
        "  };\n"
    )


def _emit_workspace_view_tsx() -> str:
    return (
        "  return (\n"
        '    <section className="records-workspace" aria-label="Application records">\n'
        '      <header className="records-session">\n'
        '        <div><p className="eyebrow">Trusted access</p><h2>Community workspace</h2>\n'
        "          <p>Permissions are enforced by the server from your signed session.</p></div>\n"
        '        {session.authenticated ? <div className="session-card">\n'
        "          <strong>{session.role}</strong><span>User #{session.user_id}</span>\n"
        '          <button type="button" onClick={() => void logout()}>'
        "Sign out</button></div> :\n"
        '          <form className="login-form" onSubmit={login}>\n'
        '            <label>Email<input type="email" required value={email}\n'
        "              onChange={(event) => setEmail(event.target.value)} /></label>\n"
        '            <label>Password<input type="password" required value={password}\n'
        "              onChange={(event) => setPassword(event.target.value)} /></label>\n"
        '            <button className="btn" type="submit">Sign in</button>\n'
        "          </form>}\n"
        "      </header>\n"
        '      {message ? <p className="records-message" role="status">{message}</p> : null}\n'
        '      {isRoleAdmin ? <section className="role-admin"><h3>User roles</h3>\n'
        "        {users.map((user) => <label key={user.id}><span>{String(user.email)}</span>\n"
        "          <select value={String(user.role)}\n"
        "            onChange={(event) => void changeRole(user.id, event.target.value)}>\n"
        "            {roleOptions}</select>\n"
        "        </label>)}\n"
        "      </section> : null}\n"
        '      <div className="records-grid">{ENTITIES.map((meta) => '
        "<EntityPanel key={meta.id} meta={meta} session={session} reload={reload} />)}</div>\n"
        "    </section>\n"
        "  );\n"
        "}\n"
    )


def emit_records_workspace_tsx(app: AppSpec) -> str:
    """Accessible generic CRUD/auth UI driven only by the validated AppSpec."""
    return (
        _emit_workspace_prelude(app)
        + _emit_entity_panel_logic_tsx()
        + _emit_entity_panel_view_tsx()
        + _emit_workspace_logic_tsx()
        + _emit_workspace_view_tsx()
    )


def records_policy_css() -> str:
    return (
        "\n/* Trusted records workspace */\n"
        ".records-workspace{max-width:1180px;margin:2rem auto;"
        "padding:clamp(1rem,3vw,2.5rem);\n"
        "  border:1px solid color-mix(in srgb,var(--primary) 18%,transparent);"
        "border-radius:24px;\n"
        "  background:color-mix(in srgb,var(--surface) 94%,var(--accent));"
        "box-shadow:0 24px 70px rgba(0,0,0,.12)}\n"
        ".records-session,.records-panel-heading,.session-card,.records-actions{\n"
        "  display:flex;gap:1rem;align-items:center;justify-content:space-between;"
        "flex-wrap:wrap}\n"
        ".login-form,.records-form{display:grid;"
        "grid-template-columns:repeat(auto-fit,minmax(170px,1fr));\n"
        "  gap:.8rem;align-items:end}\n"
        ".login-form label,.records-form label,.role-admin label{\n"
        "  display:grid;gap:.35rem;font-size:.82rem;font-weight:700}\n"
        ".login-form input,.records-form input,.role-admin select{\n"
        "  width:100%;padding:.7rem .8rem;border:1px solid "
        "color-mix(in srgb,var(--primary) 25%,transparent);\n"
        "  border-radius:10px;background:var(--surface);color:inherit}\n"
        ".records-grid{display:grid;gap:1rem;margin-top:1.4rem}\n"
        ".records-panel{padding:1.2rem;border-radius:18px;\n"
        "  background:color-mix(in srgb,var(--surface) 86%,white);\n"
        "  border:1px solid color-mix(in srgb,var(--primary) 12%,transparent)}\n"
        ".records-list{display:grid;gap:.75rem;margin-top:1rem}\n"
        ".record-card{padding:1rem;border-radius:14px;background:var(--surface);\n"
        "  border:1px solid color-mix(in srgb,var(--primary) 14%,transparent)}\n"
        ".record-card-meta{display:flex;justify-content:space-between;align-items:center}\n"
        ".record-card dl{display:grid;gap:.55rem}\n"
        ".record-card dl div{display:grid;"
        "grid-template-columns:minmax(90px,.35fr) 1fr;gap:.8rem}\n"
        ".record-card dt{opacity:.62}.record-card dd{margin:0;overflow-wrap:anywhere}\n"
        ".records-actions{justify-content:flex-start;margin-top:.8rem}\n"
        ".records-actions button,.session-card button{padding:.55rem .85rem;"
        "border-radius:9px;\n"
        "  border:1px solid color-mix(in srgb,var(--primary) 24%,transparent);"
        "cursor:pointer}\n"
        ".danger{color:#b42318}.locked-badge{padding:.25rem .55rem;border-radius:999px;\n"
        "  background:#fff1c2;color:#6b4d00;font-size:.75rem;font-weight:800}\n"
        ".records-message{padding:.7rem .9rem;border-radius:10px;\n"
        "  background:color-mix(in srgb,var(--accent) 16%,transparent)}\n"
        ".records-hint{opacity:.7}.role-admin{margin:1rem 0;padding:1rem;"
        "border-radius:14px;\n"
        "  background:color-mix(in srgb,var(--accent) 10%,transparent)}\n"
        ".role-admin label{grid-template-columns:1fr minmax(140px,.35fr);\n"
        "  align-items:center;margin:.45rem 0}\n"
        "@media(max-width:680px){.records-session{align-items:stretch}\n"
        "  .session-card{align-items:flex-start}.record-card dl div{"
        "grid-template-columns:1fr}\n"
        "  .role-admin label{grid-template-columns:1fr}}\n"
    )


__all__ = [
    "emit_records_policy_app_tsx",
    "emit_records_workspace_tsx",
    "records_policy_css",
]
