/** Shared empty-state placeholder for the canvas panes. */
export function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-full items-center justify-center px-body text-center font-ui text-[0.82rem] text-text-faint">
      {children}
    </div>
  );
}
