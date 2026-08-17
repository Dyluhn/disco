export function PreviewUnavailable({ reason }: { reason: string | undefined }) {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center justify-between border-b border-hairline px-body py-hair">
        <span className="font-mono text-[0.74rem] text-text-faint">Preview</span>
      </div>
      <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-hair px-body text-center">
        <p className="font-ui text-[0.9rem] text-text-muted">Preparing Preview</p>
        <p className="max-w-measure font-ui text-[0.8rem] text-text-faint">
          {reason ?? "A real Preview appears here when this build starts a web runtime."}
        </p>
      </div>
    </div>
  );
}
