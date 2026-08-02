import { useState } from "react";
import type { WorkflowDraftFromDescriptionResult } from "@/types/workflow";
import { DEFAULT_OUTPUT_FORMAT } from "./constants";
import type { OutputFormat } from "./constants";

/**
 * The "scalar" author-form fields: the describe/advanced flow state (description,
 * drafted result, whether the advanced editor is open) plus the simple name/card/
 * tools/skills/output/policy fields. Params, MCP mounts and verify-checks live in
 * their own hooks (they're arrays with their own add/update/remove semantics).
 */
export function useAuthorDraftFields() {
  const [description, setDescription] = useState("");
  const [result, setResult] = useState<WorkflowDraftFromDescriptionResult | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [name, setName] = useState("");
  const [card, setCard] = useState("");
  const [tools, setTools] = useState<string[]>([]);
  const [skills, setSkills] = useState<string[]>([]);
  const [outputPathTemplate, setOutputPathTemplate] = useState("outputs/{query}.md");
  const [outputFormat, setOutputFormat] = useState<OutputFormat>(DEFAULT_OUTPUT_FORMAT);
  const [finalizer, setFinalizer] = useState("");
  const [allowsWrites, setAllowsWrites] = useState(false);
  const [untrustedContent, setUntrustedContent] = useState(true);

  const cardHasBlankLine = /\n\s*\n/.test(card);

  return {
    description,
    setDescription,
    result,
    setResult,
    advancedOpen,
    setAdvancedOpen,
    name,
    setName,
    card,
    setCard,
    cardHasBlankLine,
    tools,
    setTools,
    skills,
    setSkills,
    outputPathTemplate,
    setOutputPathTemplate,
    outputFormat,
    setOutputFormat,
    finalizer,
    setFinalizer,
    allowsWrites,
    setAllowsWrites,
    untrustedContent,
    setUntrustedContent,
  };
}

export type AuthorDraftFieldsState = ReturnType<typeof useAuthorDraftFields>;
