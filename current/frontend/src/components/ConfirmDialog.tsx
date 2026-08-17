import * as Dialog from "@radix-ui/react-dialog";
import type { ReactNode } from "react";
import { useState } from "react";

/**
 * A small confirm gate for destructive actions (e.g. deleting a conversation).
 * Deletion is irreversible, so it always asks first. Quiet styling; the confirm
 * action carries the only chroma (the unsupported/danger meaning color).
 */
interface Props {
  trigger: ReactNode;
  title: string;
  description: string;
  confirmLabel: string;
  onConfirm: () => void;
  /** Gap #63: a stable, machine-readable tag distinguishing WHICH confirm flow
   *  this is (e.g. "history-delete" vs "projects-delete"). Both delete flows
   *  share this one component, so the harness otherwise can't tell which gate it
   *  drove. Stamped as `data-confirm-context` on the dialog content + both
   *  buttons; defaults to "generic". */
  confirmContext?: string;
}

export function ConfirmDialog({
  trigger,
  title,
  description,
  confirmLabel,
  onConfirm,
  confirmContext = "generic",
}: Props) {
  const [open, setOpen] = useState(false);
  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Trigger asChild>{trigger}</Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/45" />
        <Dialog.Content
          data-confirm-context={confirmContext}
          className="fixed left-1/2 top-1/2 z-50 w-[min(26rem,92vw)] -translate-x-1/2 -translate-y-1/2 rounded-card border border-hairline bg-bg p-body pmx-rise"
        >
          <Dialog.Title className="font-ui text-[0.95rem] font-semibold text-text">
            {title}
          </Dialog.Title>
          <Dialog.Description className="mt-hair font-ui text-[0.84rem] leading-snug text-text-muted">
            {description}
          </Dialog.Description>
          <div className="mt-section flex justify-end gap-inline">
            <Dialog.Close asChild>
              <button
                type="button"
                data-disco-control="confirm-dialog.cancel"
                data-confirm-context={confirmContext}
                className="rounded-control border border-hairline px-body py-hair font-ui text-[0.82rem] text-text-muted transition-colors hover:text-text"
              >
                Cancel
              </button>
            </Dialog.Close>
            <button
              type="button"
              onClick={() => {
                onConfirm();
                setOpen(false);
              }}
              data-disco-control="confirm-dialog.confirm"
              data-confirm-context={confirmContext}
              className="rounded-control bg-unsupported px-body py-hair font-ui text-[0.82rem] font-medium text-bg transition-opacity hover:opacity-90"
            >
              {confirmLabel}
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
