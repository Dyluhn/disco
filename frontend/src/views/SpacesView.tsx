import * as Dialog from "@radix-ui/react-dialog";
import {
  AlertTriangle,
  Boxes,
  FileText,
  Loader2,
  Plus,
  Search,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import { type FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { formatDate } from "@/lib/date";
import { cn } from "@/lib/cn";
import {
  useCreateSpace,
  useDeleteSpace,
  useSpace,
  useSpaces,
  useUploadSpaceDocuments,
} from "@/hooks/useSpaces";
import type { SpaceSummary } from "@/types/spaces";

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
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
                Name a persistent corpus for research grounding.
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
        <span className="truncate font-ui text-[0.9rem] text-text">{space.name}</span>
        <span className="font-ui text-[0.75rem] text-text-faint">
          {space.doc_count} {space.doc_count === 1 ? "doc" : "docs"} / {fmtBytes(space.byte_count)}
        </span>
      </button>
    </li>
  );
}

function UploadBox({ spaceId }: { spaceId: string }) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [dragging, setDragging] = useState(false);
  const [uploadCount, setUploadCount] = useState(0);
  const upload = useUploadSpaceDocuments(spaceId);

  const startUpload = (filesLike: FileList | File[]) => {
    const files = Array.from(filesLike);
    if (files.length === 0 || upload.isPending) return;
    setUploadCount(files.length);
    upload.mutate(files);
  };

  return (
    <div className="flex flex-col gap-inline">
      <div
        onDragEnter={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragOver={(e) => e.preventDefault()}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          startUpload(e.dataTransfer.files);
        }}
        className={cn(
          "flex min-h-28 flex-col items-center justify-center gap-hair rounded-card border border-dashed px-body py-section text-center transition-colors",
          dragging ? "border-accent bg-accent/5" : "border-hairline bg-surface-1",
        )}
      >
        <Upload className="size-5 text-text-faint" aria-hidden />
        <div className="font-ui text-[0.86rem] text-text">
          {upload.isPending ? `Uploading ${uploadCount} file${uploadCount === 1 ? "" : "s"}...` : "Drop documents here"}
        </div>
        <button
          type="button"
          disabled={upload.isPending}
          onClick={() => inputRef.current?.click()}
          className="rounded-control border border-hairline px-inline py-hair font-ui text-[0.8rem] text-text-muted transition-colors hover:text-text disabled:cursor-not-allowed disabled:opacity-50"
        >
          Choose files
        </button>
        <input
          ref={inputRef}
          type="file"
          multiple
          accept=".pdf,.txt,.md,.html,.htm"
          className="sr-only"
          onChange={(e) => {
            if (e.target.files) startUpload(e.target.files);
            e.currentTarget.value = "";
          }}
        />
      </div>
      {upload.error && (
        <p role="alert" className="font-ui text-[0.8rem] text-unsupported">
          {upload.error.message}
        </p>
      )}
      {upload.data?.rejected.length ? (
        <ul className="flex flex-col gap-hair">
          {upload.data.rejected.map((item) => (
            <li key={`${item.name}:${item.reason}`} className="font-ui text-[0.78rem] text-unsupported">
              {item.name}: {item.reason}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
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
            Created {formatDate(space.created_at)} / {space.doc_count} docs / {fmtBytes(space.byte_count)}
          </p>
        </div>
        <ConfirmDialog
          title="Delete this Space?"
          description="This removes the Space registry and its persisted vector namespace. Research runs will no longer be able to ground against it."
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
      </header>

      <UploadBox spaceId={space.space_id} />

      <div className="flex flex-col gap-inline">
        <h3 className="font-ui text-[0.86rem] font-medium text-text">Documents</h3>
        {space.documents.length === 0 ? (
          <div className="rounded-card border border-hairline bg-surface-1 p-body font-ui text-[0.84rem] text-text-muted">
            No documents imported yet.
          </div>
        ) : (
          <ul className="flex flex-col rounded-card border border-hairline bg-surface-1">
            {space.documents.map((doc) => (
              <li
                key={doc.document_id}
                className="flex items-center justify-between gap-inline border-b border-hairline px-body py-inline last:border-b-0"
              >
                <div className="flex min-w-0 items-center gap-inline">
                  <FileText className="size-4 shrink-0 text-text-faint" aria-hidden />
                  <div className="min-w-0">
                    <div className="truncate font-ui text-[0.86rem] text-text">{doc.name}</div>
                    <div className="font-ui text-[0.74rem] text-text-faint">
                      {doc.passage_count} passages / {fmtBytes(doc.byte_count)}
                    </div>
                  </div>
                </div>
                <span className="shrink-0 font-ui text-[0.72rem] text-text-faint">
                  {formatDate(doc.created_at)}
                </span>
              </li>
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
              Persistent corpora for Research and Deep Research grounding.
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
              <p className="p-body font-ui text-[0.84rem] text-text-muted">No Spaces match “{q}”.</p>
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
