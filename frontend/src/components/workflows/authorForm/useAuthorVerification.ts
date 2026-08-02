import { useState } from "react";
import type { KeyboardEvent } from "react";
import { compactTags } from "./draftMapping";

export function useAuthorVerification() {
  const [verifyChecks, setVerifyChecks] = useState<string[]>([]);
  const [verifyDraft, setVerifyDraft] = useState("");

  function addVerifyChecks() {
    const next = compactTags(verifyDraft);
    if (next.length === 0) return;
    setVerifyChecks((current) => Array.from(new Set([...current, ...next])));
    setVerifyDraft("");
  }

  function handleVerifyKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key !== "Enter") return;
    event.preventDefault();
    addVerifyChecks();
  }

  function removeVerifyCheck(check: string) {
    setVerifyChecks((current) => current.filter((item) => item !== check));
  }

  return {
    verifyChecks,
    setVerifyChecks,
    verifyDraft,
    setVerifyDraft,
    addVerifyChecks,
    handleVerifyKeyDown,
    removeVerifyCheck,
  };
}

export type AuthorVerificationState = ReturnType<typeof useAuthorVerification>;
