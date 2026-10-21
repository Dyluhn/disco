import { useState } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { AudioModeDialog } from "./AudioModeDialog";

describe("AudioModeDialog focus restoration", () => {
  it.each(["disabled", "aria-disabled", "inert", "removed", "available"])(
    "checks the invoker's current %s state when generation closes the dialog",
    async (state) => {
      function AudioFlow() {
        const [open, setOpen] = useState(false);
        const [generating, setGenerating] = useState(false);
        return <>
          <div inert={generating && state === "inert"}>
            {!(generating && state === "removed") && <button
              disabled={generating && state === "disabled"}
              aria-disabled={generating && state === "aria-disabled"}
              onClick={() => setOpen(true)}
            >Audio</button>}
          </div>
          <AudioModeDialog open={open} onOpenChange={setOpen} onChoose={() => {
            setOpen(false);
            setGenerating(true);
          }} />
        </>;
      }
      const user = userEvent.setup();
      render(<AudioFlow />);
      const trigger = screen.getByRole("button", { name: "Audio" });
      await user.click(trigger);
      await screen.findByRole("dialog", { name: "Generate audio overview" });
      const restore = vi.spyOn(trigger, "focus");
      await user.click(screen.getByRole("button", { name: /Single speaker/ }));
      await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
      // Radix schedules close autofocus on scope unmount; flush that task before
      // a negative assertion, so a delayed invalid focus cannot pass unnoticed.
      await new Promise((resolve) => setTimeout(resolve, 0));
      if (state === "available") {
        expect(trigger).toHaveFocus();
        expect(restore).toHaveBeenCalledWith({ preventScroll: true });
      } else {
        expect(restore).not.toHaveBeenCalled();
        expect(trigger).not.toHaveFocus();
      }
      restore.mockRestore();
    },
  );
});
