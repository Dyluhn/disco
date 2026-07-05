import * as Dialog from "@radix-ui/react-dialog";
import {
  AlertTriangle,
  Boxes,
  Folder,
  Loader2,
  Pencil,
  Plus,
  Search,
  Trash2,
  X,
} from "lucide-react";
import { type FormEvent, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { cn } from "@/lib/cn";
import { formatDate } from "@/lib/date";
import {
  useCreateSpace,
  useDeleteSpace,
  useRenameSpace,
  useSpace,
  useSpaces,
} from "@/hooks/useSpaces";
import type { ConversationSummary } from "@/types/conversation";
import type { SpaceDetail, SpaceSummary } from "@/types/spaces";

function surfaceRoute(c: ConversationSummary): string {
  if (c.origin === "imported") return `/imported/${c.id}`;
  if (c.surface === "deep_research") return `/deep/${c.id}`;
  if (c.surface === "agent") return `/agent/${c.id}`;
  if (c.surface === "build") return `/build/${c.id}`;
  return "/";
}

function SurfaceBadge({ c }: { c: ConversationSummary }) {
  const label =
    c.origin === "imported"
      ? "imported"
      : c.surface === "deep_research"
        ? "deep"
        : c.surface === "build" || c.surface === "agent"
          ? c.surface
          : null;
  if (!label) return null;
  return (
    <span className="shrink-0 rounded-full border border-hairline px-hair font-ui text-[0.62rem] uppercase tracking-wide text-text-faint">
      {label}
    </span>
  );
}

function CreateSpaceDialog() {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const create = useCreateSpace();

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const clean = name.trim();
    if (!clean) return;
    create.mutate(
      { name: clean, description },
      {
        onSuccess: () => {
          setName("");
          setDescription("");
          setOpen(false);
        },
      },
    );
  };

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Trigger asChild>
        <button
          type="button"
          data-disco-control="spaces.create"
          className="flex items-center gap-hair rounded-control bg-accent px-body py-hair font-ui text-[0.84rem] text-bg transition-opacity hover:opacity-90"
        >
          <Plus className="size-4" aria-hidden />
          New Space
        </button>
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/45" />
        <Dialog.Content className="fixed left-1/2 top-1/2 z-50 flex w-[min(32rem,92vw)] -translate-x-1/2 -translate-y-1/2 flex-col rounded-card border border-hairline bg-bg p-body pmx-rise">
          <div className="flex items-start justify-between gap-inline">
            <div>
              <Dialog.Title className="font-ui text-[0.98rem] font-semibold text-text">
                Create Space
              </Dialog.Title>
              <Dialog.Description className="mt-hair font-ui text-[0.8rem] text-text-muted">
                Create a folder for related conversations and runs.
              </Dialog.Description>
            </div>
            <Dialog.Close asChild>
              <button
                type="button"
                aria-label="Close"
                className="grid size-7 place-items-center rounded-control text-text-faint transition-colors hover:bg-surface-2 hover:text-text"
              >
                <X className="size-4" aria-hidden />
              </button>
            </Dialog.Close>
          </div>
          <form onSubmit={submit} className="mt-body flex flex-col gap-inline">
            <label className="flex flex-col gap-hair font-ui text-[0.78rem] text-text-muted">
              Name
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                autoFocus
                className="rounded-control border border-hairline bg-surface-1 px-inline py-hair text-[0.9rem] text-text outline-none focus:border-hairline-strong"
              />
            </label>
            <label className="flex flex-col gap-hair font-ui text-[0.78rem] text-text-muted">
              Description
              <textarea
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                rows={3}
                className="resize-none rounded-control border border-hairline bg-surface-1 px-inline py-hair text-[0.9rem] text-text outline-none focus:border-hairline-strong"
              />
            </label>
            {create.error && (
              <p role="alert" className="font-ui text-[0.8rem] text-unsupported">
                {create.error instanceof Error ? create.error.message : "Could not create Space."}
              </p>
            )}
            <div className="flex justify-end gap-inline pt-inline">
              <Dialog.Close asChild>
                <button
                  type="button"
                  className="rounded-control border border-hairline px-body py-hair font-ui text-[0.84rem] text-text-muted transition-colors hover:text-text"
                >
                  Cancel
                </button>
              </Dialog.Close>
              <button
                type="submit"
                disabled={!name.trim() || create.isPending}
                className="flex items-center gap-hair rounded-control bg-accent px-body py-hair font-ui text-[0.84rem] text-bg transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {create.isPending && <Loader2 className="size-3.5 animate-spin" aria-hidden />}
                Create
              </button>
            </div>
          </form>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function RenameSpaceDialog({ space }: { space: SpaceDetail }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState(space.name);
  const [description, setDescription] = useState(space.description);
  const rename = useRenameSpace();

  useEffect(() => {
    if (!open) {
      setName(space.name);
      setDescription(space.description);
    }
  }, [open, space.name, space.description]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const clean = name.trim();
    if (!clean) return;
    rename.mutate(
      { spaceId: space.space_id, name: clean, description },
      { onSuccess: () => setOpen(false) },
    );
  };

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Trigger asChild>
        <button
          type="button"
          aria-label={`Rename Space: ${space.name}`}
          data-disco-control="spaces.rename"
          className="grid size-8 place-items-center rounded-control border border-hairline text-text-faint transition-colors hover:text-text"
        >
          <Pencil className="size-4" aria-hidden />
        </button>
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/45" />
        <Dialog.Content className="fixed left-1/2 top-1/2 z-50 flex w-[min(32rem,92vw)] -translate-x-1/2 -translate-y-1/2 flex-col rounded-card border border-hairline bg-bg p-body pmx-rise">
          <div className="flex items-start justify-between gap-inline">
            <Dialog.Title className="font-ui text-[0.98rem] font-semibold text-text">
              Rename Space
            </Dialog.Title>
            <Dialog.Close asChild>
              <button
                type="button"
                aria-label="Close"
                className="grid size-7 place-items-center rounded-control text-text-faint transition-colors hover:bg-surface-2 hover:text-text"
              >
                <X className="size-4" aria-hidden />
              </button>
            </Dialog.Close>
          </div>
          <form onSubmit={submit} className="mt-body flex flex-col gap-inline">
            <label className="flex flex-col gap-hair font-ui text-[0.78rem] text-text-muted">
              Name
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                autoFocus
                className="rounded-control border border-hairline bg-surface-1 px-inline py-hair text-[0.9rem] text-text outline-none focus:border-hairline-strong"
              />
            </label>
            <label className="flex flex-col gap-hair font-ui text-[0.78rem] text-text-muted">
              Description
              <textarea
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                rows={3}
                className="resize-none rounded-control border border-hairline bg-surface-1 px-inline py-hair text-[0.9rem] text-text outline-none focus:border-hairline-strong"
              />
            </label>
            {rename.error && (
              <p role="alert" className="font-ui text-[0.8rem] text-unsupported">
                {rename.error instanceof Error ? rename.error.message : "Could not rename Space."}
              </p>
            )}
            <div className="flex justify-end gap-inline pt-inline">
              <Dialog.Close asChild>
                <button
                  type="button"
                  className="rounded-control border border-hairline px-body py-hair font-ui text-[0.84rem] text-text-muted transition-colors hover:text-text"
                >
                  Cancel
                </button>
              </Dialog.Close>
              <button
                type="submit"
                disabled={!name.trim() || rename.isPending}
                className="flex items-center gap-hair rounded-control bg-accent px-body py-hair font-ui text-[0.84rem] text-bg transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {rename.isPending && <Loader2 className="size-3.5 animate-spin" aria-hidden />}
                Save
              </button>
            </div>
          </form>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function SpaceRow({
  space,
  active,
  onSelect,
}: {
  space: SpaceSummary;
  active: boolean;
  onSelect: () => void;
}) {
  return (
    <li>
      <button
        type="button"
        onClick={onSelect}
        data-disco-control="spaces.open-row"
        className={cn(
          "flex w-full flex-col gap-hair border-b border-hairline px-inline py-inline text-left transition-colors last:border-b-0",
          active ? "bg-surface-2" : "hover:bg-surface-1",
        )}
      >
        <span className="flex min-w-0 items-center gap-hair">
          <Folder className="size-4 shrink-0 text-text-faint" aria-hidden />
          <span className="truncate font-ui text-[0.9rem] text-text">{space.name}</span>
        </span>
        <span className="font-ui text-[0.75rem] text-text-faint">
          {space.member_count} {space.member_count === 1 ? "item" : "items"}
        </span>
      </button>
    </li>
  );
}

function MemberRow({ c }: { c: ConversationSummary }) {
  const navigate = useNavigate();
  return (
    <li className="flex items-center justify-between gap-inline border-b border-hairline px-body py-inline last:border-b-0">
      <button
        type="button"
        data-disco-control="spaces.open-member"
        data-conversation-id={c.id}
        onClick={() => navigate(surfaceRoute(c))}
        className="group min-w-0 flex-1 text-left"
      >
        <div className="flex min-w-0 items-center gap-hair">
          <span className="truncate font-ui text-[0.9rem] text-text transition-colors group-hover:text-accent">
            {c.title}
          </span>
          <SurfaceBadge c={c} />
        </div>
        <div className="font-ui text-[0.76rem] text-text-faint">
          {formatDate(c.created_at)}
        </div>
      </button>
    </li>
  );
}

function SpaceDetailPane({ spaceId }: { spaceId: string | null }) {
  const { data: space, isLoading, isError, refetch } = useSpace(spaceId);
  const del = useDeleteSpace();

  if (!spaceId) {
    return (
      <div className="flex min-h-64 flex-col items-center justify-center gap-inline rounded-card border border-hairline bg-surface-1 p-major text-center">
        <Boxes className="size-6 text-text-faint" aria-hidden />
        <p className="font-ui text-[0.88rem] text-text-muted">Select a Space to inspect it.</p>
      </div>
    );
  }

  if (isLoading) {
    return (
      <div className="flex min-h-64 items-center justify-center rounded-card border border-hairline bg-surface-1">
        <Loader2 className="size-5 animate-spin text-text-faint" aria-hidden />
      </div>
    );
  }

  if (isError || !space) {
    return (
      <div role="alert" className="flex min-h-64 flex-col items-center justify-center gap-inline rounded-card border border-hairline bg-surface-1 p-major text-center">
        <AlertTriangle className="size-5 text-warn" aria-hidden />
        <p className="font-ui text-[0.88rem] text-text-muted">Could not load this Space.</p>
        <button
          type="button"
          onClick={() => refetch()}
          className="rounded-control border border-hairline px-body py-hair font-ui text-[0.82rem] text-text-muted transition-colors hover:text-text"
        >
          Try again
        </button>
      </div>
    );
  }

  return (
    <section className="flex min-w-0 flex-col gap-section">
      <header className="flex flex-wrap items-start justify-between gap-inline border-b border-hairline pb-section">
        <div className="min-w-0">
          <h2 className="truncate font-display text-[1.6rem] text-text">{space.name}</h2>
          {space.description && (
            <p className="mt-hair max-w-measure font-ui text-[0.84rem] text-text-muted">
              {space.description}
            </p>
          )}
          <p className="mt-hair font-ui text-[0.76rem] text-text-faint">
            Created {formatDate(space.created_at)} / {space.member_count}{" "}
            {space.member_count === 1 ? "item" : "items"}
          </p>
        </div>
        <div className="flex items-center gap-hair">
          <RenameSpaceDialog space={space} />
          <ConfirmDialog
            title="Delete this Space?"
            description="Conversations stay in History and become Unfiled."
            confirmLabel="Delete"
            confirmContext="space-delete"
            onConfirm={() => del.mutate(space.space_id)}
            trigger={
              <button
                type="button"
                aria-label={`Delete Space: ${space.name}`}
                className="grid size-8 place-items-center rounded-control border border-hairline text-text-faint transition-colors hover:border-unsupported/50 hover:text-unsupported"
              >
                <Trash2 className="size-4" aria-hidden />
              </button>
            }
          />
        </div>
      </header>

      <div className="flex flex-col gap-inline">
        <h3 className="font-ui text-[0.86rem] font-medium text-text">Members</h3>
        {space.members.length === 0 ? (
          <div className="rounded-card border border-hairline bg-surface-1 p-body font-ui text-[0.84rem] text-text-muted">
            No items yet — move conversations here from History.
          </div>
        ) : (
          <ul className="flex flex-col rounded-card border border-hairline bg-surface-1">
            {space.members.map((member) => (
              <MemberRow key={member.id} c={member} />
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

export function SpacesView() {
  const { data, isLoading, isError, refetch } = useSpaces();
  const spaces = useMemo(() => data?.spaces ?? [], [data?.spaces]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [q, setQ] = useState("");

  useEffect(() => {
    if (spaces.length === 0) {
      setSelectedId(null);
      return;
    }
    if (!selectedId || !spaces.some((space) => space.space_id === selectedId)) {
      setSelectedId(spaces[0].space_id);
    }
  }, [spaces, selectedId]);

  const filtered = spaces.filter((space) =>
    `${space.name} ${space.description}`.toLowerCase().includes(q.trim().toLowerCase()),
  );

  return (
    <div className="mx-auto w-full max-w-doc px-body py-section">
      <div className="mx-auto flex w-full max-w-[58rem] flex-col gap-section">
        <header className="flex flex-wrap items-end justify-between gap-inline">
          <div>
            <h1 className="font-display text-[2rem] tracking-tight text-text">Spaces</h1>
            <p className="font-ui text-[0.88rem] text-text-muted">
              Folders for organizing conversations, builds, and agent runs.
            </p>
          </div>
          <CreateSpaceDialog />
        </header>

        {isError && (
          <div role="alert" className="flex items-center justify-between gap-inline rounded-card border border-hairline bg-surface-1 p-body">
            <span className="flex items-center gap-inline font-ui text-[0.86rem] text-text-muted">
              <AlertTriangle className="size-4 text-warn" aria-hidden />
              Could not load Spaces.
            </span>
            <button
              type="button"
              onClick={() => refetch()}
              className="rounded-control border border-hairline px-inline py-hair font-ui text-[0.8rem] text-text-muted transition-colors hover:text-text"
            >
              Try again
            </button>
          </div>
        )}

        <div className="grid min-h-[32rem] gap-section lg:grid-cols-[18rem_minmax(0,1fr)]">
          <aside className="flex min-h-0 flex-col rounded-card border border-hairline bg-surface-1">
            <div className="flex items-center gap-hair border-b border-hairline px-inline py-hair">
              <Search className="size-4 shrink-0 text-text-faint" aria-hidden />
              <input
                type="search"
                value={q}
                onChange={(e) => setQ(e.target.value)}
                placeholder="Search Spaces..."
                aria-label="Search Spaces"
                className="min-w-0 flex-1 bg-transparent font-ui text-[0.84rem] text-text outline-none placeholder:text-text-faint"
              />
            </div>
            {isLoading ? (
              <div className="flex flex-col gap-inline p-inline" aria-hidden>
                {[0, 1, 2].map((i) => (
                  <div key={i} className="flex flex-col gap-hair">
                    <span className="h-3 w-2/3 animate-pulse rounded bg-surface-2" />
                    <span className="h-2 w-20 animate-pulse rounded bg-surface-2" />
                  </div>
                ))}
              </div>
            ) : spaces.length === 0 ? (
              <div className="flex flex-1 flex-col items-center justify-center gap-hair p-body text-center">
                <Boxes className="size-6 text-text-faint" aria-hidden />
                <p className="font-ui text-[0.84rem] text-text-muted">No Spaces yet.</p>
              </div>
            ) : filtered.length === 0 ? (
              <p className="p-body font-ui text-[0.84rem] text-text-muted">No Spaces match "{q}".</p>
            ) : (
              <ul className="min-h-0 overflow-y-auto">
                {filtered.map((space) => (
                  <SpaceRow
                    key={space.space_id}
                    space={space}
                    active={space.space_id === selectedId}
                    onSelect={() => setSelectedId(space.space_id)}
                  />
                ))}
              </ul>
            )}
          </aside>
          <SpaceDetailPane spaceId={selectedId} />
        </div>
      </div>
    </div>
  );
}
