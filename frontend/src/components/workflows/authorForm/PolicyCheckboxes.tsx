interface PolicyCheckboxesProps {
  allowsWrites: boolean;
  onAllowsWritesChange: (value: boolean) => void;
  untrustedContent: boolean;
  onUntrustedContentChange: (value: boolean) => void;
}

export function PolicyCheckboxes({
  allowsWrites,
  onAllowsWritesChange,
  untrustedContent,
  onUntrustedContentChange,
}: PolicyCheckboxesProps) {
  return (
    <section className="grid gap-hair md:grid-cols-2">
      <label className="flex min-h-11 items-start gap-hair rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.8rem] text-text-muted lg:min-h-0">
        <input
          type="checkbox"
          checked={allowsWrites}
          onChange={(event) => onAllowsWritesChange(event.target.checked)}
          className="mt-[0.2rem]"
        />
        <span>
          <span className="block text-text">Allows writes</span>
          <span>Required if any MCP mount is writable.</span>
        </span>
      </label>
      <label className="flex min-h-11 items-start gap-hair rounded-control border border-hairline bg-surface-2 px-inline py-hair font-ui text-[0.8rem] text-text-muted lg:min-h-0">
        <input
          type="checkbox"
          checked={untrustedContent}
          onChange={(event) => onUntrustedContentChange(event.target.checked)}
          className="mt-[0.2rem]"
        />
        <span>
          <span className="block text-text">Untrusted content</span>
          <span>Default on for user and connector-provided inputs.</span>
        </span>
      </label>
    </section>
  );
}
