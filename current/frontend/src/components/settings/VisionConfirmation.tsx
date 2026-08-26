import * as Dialog from "@radix-ui/react-dialog";

export function VisionConfirmation({
  open,
  onOpenChange,
  onChoose,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onChoose: (vision: boolean) => void;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-[60] bg-black/45" />
        <Dialog.Content className="fixed left-1/2 top-1/2 z-[70] w-[min(26rem,92vw)] -translate-x-1/2 -translate-y-1/2 rounded-card border border-hairline bg-bg p-body pmx-rise">
          <Dialog.Title className="font-ui text-[0.95rem] font-semibold text-text">
            Does this model support images?
          </Dialog.Title>
          <Dialog.Description className="mt-hair font-ui text-[0.8rem] leading-snug text-text-muted">
            The provider did not report a capability and the model name is not
            conclusive. Choose the endpoint&apos;s actual input modality before saving.
          </Dialog.Description>
          <div className="mt-body grid grid-cols-2 gap-inline">
            <button
              type="button"
              className="min-h-11 rounded-control bg-accent px-inline py-hair font-ui text-[0.8rem] font-medium text-bg"
              onClick={() => onChoose(true)}
            >
              Supports images
            </button>
            <button
              type="button"
              className="min-h-11 rounded-control border border-hairline px-inline py-hair font-ui text-[0.8rem] text-text-muted hover:text-text"
              onClick={() => onChoose(false)}
            >
              Text only
            </button>
          </div>
          <Dialog.Close asChild>
            <button
              type="button"
              className="mt-inline w-full font-ui text-[0.78rem] text-text-faint hover:text-text"
            >
              Cancel — do not add
            </button>
          </Dialog.Close>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
