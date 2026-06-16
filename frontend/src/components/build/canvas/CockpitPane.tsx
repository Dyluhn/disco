import { Cpu, FileCode2, ServerCog, SquareTerminal } from "lucide-react";
import { cn } from "@/lib/cn";
import type { SessionInfo, SessionView } from "@/hooks/useSessions";
import type { AgentEvent, PreviewInfo } from "@/types/agent";
import { TerminalHistoryView } from "./TerminalPane";

/** The known user-facing port set (mirror of `packages/.../sandbox/_container.py
 *  USER_PORTS = {8000, 3000, 5173, 8080, 5000, 4321}`). Kept client-side as a
 *  type-level label for cockpit rows; the authoritative set is the backend
 *  response (we only RENDER the rows the backend says are bound — no
 *  guessing). */
const KNOWN_USER_PORTS: readonly number[] = [8000, 3000, 5173, 8080, 5000, 4321];

// ---- Cockpit (BP-G12) -------------------------------------------------------
//
// The build "cockpit" — a build operator's eye-view that consolidates the EXISTING
// shell_view output + running-server status (bound USER_PORTS) + process list into
// a single always-visible surface, so the user can SEE the box (no clicking
// through tabs to confirm "is it actually up"). Fed entirely by the data the
// backend already exposes via `shell_view` (the `useSessions` polling) and the
// existing `getPreview` ports/owner payload (the `useBuildPreview` hook) — no new
// backend. Honest by construction: if a port isn't bound or no process owns a
// port, the section shows an empty state, not a fabricated row.

/** One cockpit sub-section: a labelled box (icon + title + subtitle + body). The
 *  three sections (Shell / Servers / Processes) share the chrome so a glance at
 *  the column reads as one operator dashboard, not three ad-hoc panels. */
function CockpitSection({
  icon: Icon,
  title,
  subtitle,
  count,
  children,
}: {
  icon: typeof FileCode2;
  title: string;
  subtitle: string;
  /** An optional numeric badge — the "N running" / "N bound" / "N processes"
   *  affordance the operator scans first. null/undefined hides it. */
  count?: number | null;
  children: React.ReactNode;
}) {
  return (
    <section
      aria-label={title}
      className="flex shrink-0 flex-col border-b border-hairline last:border-b-0"
    >
      <header className="flex items-baseline justify-between gap-inline border-b border-hairline bg-surface-1 px-body py-hair">
        <span className="flex items-center gap-hair font-ui text-[0.78rem] text-text">
          <Icon className="size-3.5 text-text-muted" aria-hidden />
          {title}
          {count != null && (
            <span
              className="ml-hair rounded-full border border-hairline px-inline py-px font-mono text-[0.66rem] text-text-muted"
              aria-label={`${count}`}
            >
              {count}
            </span>
          )}
        </span>
        <span className="truncate font-ui text-[0.7rem] text-text-faint">{subtitle}</span>
      </header>
      <div className="bg-bg">{children}</div>
    </section>
  );
}

/** The "running" / "idle" / "bound" / "free" status dot — one visual language for
 *  the three sections. Green = active, gray = idle, dim = empty slot. */
function CockpitDot({ kind }: { kind: "running" | "idle" | "bound" | "free" }) {
  const cls =
    kind === "running" || kind === "bound"
      ? "bg-[oklch(0.75_0.18_150)] animate-pulse"
      : kind === "idle"
        ? "bg-[oklch(0.55_0.08_150)]"
        : "bg-hairline";
  return (
    <span
      className={cn("size-1.5 rounded-full", cls)}
      aria-label={kind}
      title={kind}
    />
  );
}

/** Cockpit — Shell section: session chips + the live view of the selected one.
 *  The "history fallback" for `sessions.length === 0` reuses the same
 *  `TerminalHistoryView` the Terminal pane uses, so the operator sees the same
 *  past commands even before the agent spawns a live session. */
function CockpitShellSection({
  events,
  sessions,
  selectedName,
  setSelectedName,
  view,
}: {
  events: AgentEvent[];
  sessions: SessionInfo[];
  selectedName: string | null;
  setSelectedName: (name: string) => void;
  view: SessionView | null;
}) {
  const busyCount = sessions.filter((s) => s.busy).length;
  return (
    <CockpitSection
      icon={SquareTerminal}
      title="Shell"
      subtitle="Live tmux sessions (shell_view)"
      count={sessions.length > 0 ? sessions.length : null}
    >
      {sessions.length === 0 ? (
        <div className="px-body py-hair">
          <TerminalHistoryView events={events} />
        </div>
      ) : (
        <>
          <div className="flex shrink-0 items-center gap-hair overflow-x-auto border-b border-hairline px-body py-hair font-mono text-[0.72rem]">
            {sessions.map((s) => (
              <button
                key={s.name}
                type="button"
                onClick={() => setSelectedName(s.name)}
                className={cn(
                  "flex shrink-0 items-center gap-hair rounded px-inline py-px transition-colors",
                  s.name === selectedName
                    ? "bg-surface-2 text-text"
                    : "text-text-faint hover:text-text",
                )}
                title={s.last_line || s.name}
              >
                <CockpitDot kind={s.busy ? "running" : "idle"} />
                <span>{s.name}</span>
                {s.busy && (
                  <span className="rounded-full border border-accent/30 px-inline py-px text-[0.62rem] text-accent">
                    running
                  </span>
                )}
              </button>
            ))}
          </div>
          <div
            className="min-h-0 overflow-auto bg-[oklch(0.15_0.005_260)] px-body py-inline"
            style={{ maxHeight: 240 }}
          >
            <pre className="whitespace-pre-wrap font-mono text-[0.76rem] leading-relaxed text-[oklch(0.78_0.005_260)]">
              {view?.content || "(no output yet)"}
            </pre>
          </div>
          <div className="border-t border-hairline px-body py-hair font-mono text-[0.7rem] text-text-faint">
            {busyCount > 0
              ? `${busyCount} of ${sessions.length} session${sessions.length === 1 ? "" : "s"} running`
              : `${sessions.length} session${sessions.length === 1 ? "" : "s"} idle`}
          </div>
        </>
      )}
    </CockpitSection>
  );
}

/** Cockpit — Servers section: the bound USER_PORTS, one per row, with the
 *  owning process surfaced. The set of KNOWN_USER_PORTS is fixed by the
 *  backend's allowlist (`sandbox/_container.py USER_PORTS`), so we render one
 *  row per port with a `bound` / `free` dot — the operator sees at a glance
 *  "is the dev server on :8000 yet?". We only ever show a server row's owner
 *  when the backend reports one (no fabrication). */
function CockpitServersSection({ data }: { data: PreviewInfo | null | undefined }) {
  // Build the canonical row set keyed on KNOWN_USER_PORTS so the operator sees
  // a stable layout: "the port is the row, the owner is what fills it." The
  // backend's `data.ports` may contain only the bound subset; we fill the rest
  // as `free` from the canonical list.
  const ports = data?.ports ?? [];
  const portByNumber = new Map(ports.map((p) => [p.port, p]));
  const knownBound = KNOWN_USER_PORTS.map((port) => ({
    port,
    entry: portByNumber.get(port) ?? null,
  }));
  // Include any backend-reported port outside the canonical set (forward-compat:
  // the backend adds a port, we surface it rather than swallow it).
  const extras = ports.filter((p) => !KNOWN_USER_PORTS.includes(p.port));
  const rows = [
    ...knownBound.map((r) => ({ port: r.port, owner: r.entry?.owner ?? null })),
    ...extras.map((p) => ({ port: p.port, owner: p.owner })),
  ];
  const boundCount = rows.filter((r) => r.owner != null).length;
  return (
    <CockpitSection
      icon={ServerCog}
      title="Servers"
      subtitle="Bound USER_PORTS + their owning process"
      count={rows.length > 0 ? boundCount : null}
    >
      {rows.length === 0 ? (
        <div className="px-body py-inline font-ui text-[0.76rem] text-text-faint">
          No USER_PORTS are known to this build.
        </div>
      ) : (
        <ul className="divide-y divide-hairline font-mono text-[0.74rem]">
          {rows.map((r) => (
            <li
              key={r.port}
              data-testid={`cockpit-port-${r.port}`}
              className="flex items-center gap-inline px-body py-hair"
            >
              <CockpitDot kind={r.owner ? "bound" : "free"} />
              <span className="w-14 shrink-0 text-text">:{r.port}</span>
              {r.owner ? (
                <span className="min-w-0 flex-1 truncate" title={r.owner.cmdline}>
                  <span className="text-text-muted">pid {r.owner.pid}</span>
                  <span className="px-hair text-text-faint">·</span>
                  <span className="text-text">{r.owner.cmdline}</span>
                  {r.owner.session && (
                    <>
                      <span className="px-hair text-text-faint">·</span>
                      <span className="text-text-faint">session {r.owner.session}</span>
                    </>
                  )}
                </span>
              ) : (
                <span className="text-text-faint">free</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </CockpitSection>
  );
}

/** Cockpit — Processes section: the distinct processes (deduped by pid) that
 *  own at least one bound USER_PORT. This is a different facet from Servers
 *  (which is keyed on the port) — a single process may bind multiple ports,
 *  and the operator wants to see "what's actually running on this box" not
 *  just "is :8000 listening". */
function CockpitProcessesSection({ data }: { data: PreviewInfo | null | undefined }) {
  const ports = data?.ports ?? [];
  const seen = new Set<number>();
  const procs: Array<{
    pid: number;
    cmdline: string;
    session: string | null;
    ports: number[];
  }> = [];
  for (const p of ports) {
    if (!p.owner) continue;
    if (seen.has(p.owner.pid)) {
      // append this port to the existing process row
      const existing = procs.find((x) => x.pid === p.owner!.pid)!;
      if (!existing.ports.includes(p.port)) existing.ports.push(p.port);
      continue;
    }
    seen.add(p.owner.pid);
    procs.push({
      pid: p.owner.pid,
      cmdline: p.owner.cmdline,
      session: p.owner.session,
      ports: [p.port],
    });
  }
  // Stable sort: by pid ascending — a familiar "ps"-style ordering.
  procs.sort((a, b) => a.pid - b.pid);
  return (
    <CockpitSection
      icon={Cpu}
      title="Processes"
      subtitle="Distinct processes bound to USER_PORTS"
      count={procs.length > 0 ? procs.length : null}
    >
      {procs.length === 0 ? (
        <div className="px-body py-inline font-ui text-[0.76rem] text-text-faint">
          No processes detected. The agent has not yet bound a USER_PORT.
        </div>
      ) : (
        <ul className="divide-y divide-hairline font-mono text-[0.74rem]">
          {procs.map((p) => (
            <li
              key={p.pid}
              data-testid={`cockpit-proc-${p.pid}`}
              className="flex items-center gap-inline px-body py-hair"
            >
              <CockpitDot kind="bound" />
              <span className="w-20 shrink-0 text-text">pid {p.pid}</span>
              <span className="min-w-0 flex-1 truncate" title={p.cmdline}>
                {p.cmdline}
              </span>
              <span className="shrink-0 text-text-faint">
                :{[...p.ports].sort((a, b) => a - b).join(", :")}
              </span>
              {p.session && (
                <span className="shrink-0 text-text-faint">· {p.session}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </CockpitSection>
  );
}

/** The combined Cockpit pane — three sections stacked, all from existing data. */
export function CockpitPane({
  events,
  sessions,
  selectedName,
  setSelectedName,
  view,
  data,
}: {
  events: AgentEvent[];
  sessions: SessionInfo[];
  selectedName: string | null;
  setSelectedName: (name: string) => void;
  view: SessionView | null;
  data: PreviewInfo | null | undefined;
}) {
  return (
    <div className="flex h-full min-h-0 flex-col overflow-auto bg-bg" data-testid="cockpit-pane">
      <CockpitShellSection
        events={events}
        sessions={sessions}
        selectedName={selectedName}
        setSelectedName={setSelectedName}
        view={view}
      />
      <CockpitServersSection data={data} />
      <CockpitProcessesSection data={data} />
    </div>
  );
}
