import { useState } from "react";
import { Pencil, Plus, Trash2 } from "lucide-react";
import { cn } from "@/lib/cn";
import {
  useCreateSkill,
  useDeleteSkill,
  useSkills,
  useUpdateSkill,
} from "@/hooks/useConfig";

/**
 * Skills — reusable instruction modules handed to the Build agent (Claude-Code
 * style SKILL.md). Each skill is a markdown file with a name, a one-line
 * description, an enabled toggle, and a body of instructions the agent follows.
 * Real + persistent: backed by .md files on the server (or localStorage offline),
 * so skills survive reloads. Enabled skills are injected into the agent's context.
 */

function Switch({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      onClick={() => onChange(!checked)}
      className={cn(
        "relative h-5 w-9 shrink-0 rounded-full border transition-colors",
        checked ? "border-accent/50 bg-accent/30" : "border-hairline bg-surface-2",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "absolute top-1/2 size-3.5 -translate-y-1/2 rounded-full transition-all",
          checked ? "left-[1.15rem] bg-accent" : "left-[0.15rem] bg-text-faint",
        )}
      />
    </button>
  );
}

interface DraftState {
  name: string;
  description: string;
  body: string;
}

const EMPTY_DRAFT: DraftState = { name: "", description: "", body: "" };

function SkillEditor({
  initial,
  busy,
  onSave,
  onCancel,
}: {
  initial: DraftState;
  busy: boolean;
  onSave: (draft: DraftState) => void;
  onCancel: () => void;
}) {
  const [draft, setDraft] = useState<DraftState>(initial);
  const canSave = draft.name.trim().length > 0 && draft.body.trim().length > 0;
  return (
    <div className="flex flex-col gap-inline rounded-card border border-accent/40 bg-surface-1 p-body">
      <input
        value={draft.name}
        onChange={(e) => setDraft({ ...draft, name: e.target.value })}
        placeholder="Skill name (e.g. Yahoo Finance API)"
        aria-label="Skill name"
        className="rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.88rem] text-text outline-none focus:border-accent"
      />
      <input
        value={draft.description}
        onChange={(e) => setDraft({ ...draft, description: e.target.value })}
        placeholder="One-line description (what this skill is for)"
        aria-label="Skill description"
        className="rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.82rem] text-text outline-none focus:border-accent"
      />
      <textarea
        value={draft.body}
        onChange={(e) => setDraft({ ...draft, body: e.target.value })}
        placeholder={
          "Markdown instructions the agent will follow…\n\ne.g.\n## Fetching stock data\nUse the Yahoo v8 chart endpoint with a Mozilla User-Agent header."
        }
        aria-label="Skill instructions (markdown)"
        rows={8}
        className="resize-y rounded-control border border-hairline bg-surface-2 px-inline py-hair font-mono text-[0.8rem] leading-relaxed text-text outline-none focus:border-accent"
      />
      <div className="flex items-center justify-end gap-inline">
        <button
          type="button"
          onClick={onCancel}
          className="rounded-control px-inline py-hair font-ui text-[0.8rem] text-text-muted hover:text-text"
        >
          Cancel
        </button>
        <button
          type="button"
          disabled={!canSave || busy}
          onClick={() => onSave(draft)}
          className="rounded-control bg-accent px-body py-hair font-ui text-[0.8rem] font-medium text-bg transition-opacity disabled:opacity-40"
        >
          {busy ? "Saving…" : "Save skill"}
        </button>
      </div>
    </div>
  );
}

export function SkillsSection() {
  const { data: skills, isLoading } = useSkills();
  const create = useCreateSkill();
  const update = useUpdateSkill();
  const remove = useDeleteSkill();
  const [creating, setCreating] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);

  const closeEditors = () => {
    setCreating(false);
    setEditingId(null);
  };

  return (
    <section aria-labelledby="skills-heading" className="flex flex-col gap-inline">
      <div className="flex items-center justify-between gap-inline">
        <h2 id="skills-heading" className="font-ui text-[1.05rem] font-semibold text-text">
          Skills
        </h2>
        {!creating && editingId === null && (
          <button
            type="button"
            onClick={() => setCreating(true)}
            className="flex items-center gap-hair rounded-control border border-hairline px-inline py-hair font-ui text-[0.78rem] text-text-muted transition-colors hover:border-accent hover:text-text"
          >
            <Plus className="size-3.5" aria-hidden />
            New skill
          </button>
        )}
      </div>
      <p className="font-ui text-[0.84rem] text-text-muted">
        Reusable instruction files (like Claude Code's SKILL.md). Enabled skills
        are handed to the Build agent so it follows your standing guidance — API
        recipes, house style, conventions — without you re-explaining each run.
        Saved across reloads.
      </p>

      {creating && (
        <SkillEditor
          initial={EMPTY_DRAFT}
          busy={create.isPending}
          onCancel={closeEditors}
          onSave={(draft) =>
            create.mutate(
              { name: draft.name, description: draft.description, body: draft.body },
              { onSuccess: closeEditors },
            )
          }
        />
      )}

      <ul className="flex flex-col gap-inline">
        {isLoading && (
          <li className="rounded-card border border-hairline bg-surface-1 px-body py-body font-ui text-[0.84rem] text-text-muted">
            Loading skills…
          </li>
        )}
        {!isLoading && (skills ?? []).length === 0 && !creating && (
          <li className="rounded-card border border-dashed border-hairline bg-surface-1 px-body py-body text-center font-ui text-[0.84rem] text-text-faint">
            No skills yet. Create one to give the agent reusable instructions.
          </li>
        )}
        {(skills ?? []).map((s) =>
          editingId === s.id ? (
            <li key={s.id}>
              <SkillEditor
                initial={{ name: s.name, description: s.description, body: s.body ?? "" }}
                busy={update.isPending}
                onCancel={closeEditors}
                onSave={(draft) =>
                  update.mutate(
                    {
                      id: s.id,
                      patch: {
                        name: draft.name,
                        description: draft.description,
                        body: draft.body,
                      },
                    },
                    { onSuccess: closeEditors },
                  )
                }
              />
            </li>
          ) : (
            <li
              key={s.id}
              className={cn(
                "flex items-start justify-between gap-section rounded-card border bg-surface-1 px-body py-inline",
                s.enabled ? "border-hairline" : "border-hairline/50 opacity-70",
              )}
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-hair">
                  <span className="font-ui text-[0.88rem] font-medium text-text">{s.name}</span>
                  {!s.enabled && (
                    <span className="font-ui text-[0.68rem] uppercase tracking-wide text-text-faint">
                      disabled
                    </span>
                  )}
                </div>
                {s.description && (
                  <p className="font-ui text-[0.8rem] leading-snug text-text-muted">
                    {s.description}
                  </p>
                )}
                {s.body && (
                  <p className="mt-px line-clamp-2 font-mono text-[0.72rem] leading-snug text-text-faint">
                    {s.body}
                  </p>
                )}
              </div>
              <div className="flex shrink-0 items-center gap-inline">
                <button
                  type="button"
                  onClick={() => setEditingId(s.id)}
                  aria-label={`Edit ${s.name}`}
                  className="text-text-faint transition-colors hover:text-text"
                >
                  <Pencil className="size-3.5" aria-hidden />
                </button>
                <button
                  type="button"
                  onClick={() => remove.mutate(s.id)}
                  aria-label={`Delete ${s.name}`}
                  className="text-text-faint transition-colors hover:text-unsupported"
                >
                  <Trash2 className="size-3.5" aria-hidden />
                </button>
                <Switch
                  checked={s.enabled}
                  onChange={(next) => update.mutate({ id: s.id, patch: { enabled: next } })}
                  label={`Enable ${s.name}`}
                />
              </div>
            </li>
          ),
        )}
      </ul>
    </section>
  );
}
