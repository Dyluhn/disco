import { useToast } from "@/components/toastApi";

export function usePreviewOpenError() {
  const toast = useToast();
  return (reason: "popup_blocked" | "capability_unavailable") => {
    toast.show({
      title: reason === "popup_blocked" ? "Preview popup blocked" : "Couldn’t open Preview",
      body: reason === "popup_blocked"
        ? "Allow popups for this site, then try again."
        : "Isolated Preview access could not be established. Try again.",
    });
  };
}
