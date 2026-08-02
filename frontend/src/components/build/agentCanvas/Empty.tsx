export function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-hair px-body text-center font-ui text-[0.82rem] text-text-faint">
      {children}
    </div>
  );
}
