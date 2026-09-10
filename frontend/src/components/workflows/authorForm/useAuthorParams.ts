import { useState } from "react";
import type { WorkflowDraftParam } from "@/types/workflow";
import { DEFAULT_PARAM_TYPE } from "./constants";

export function useAuthorParams() {
  const [params, setParams] = useState<WorkflowDraftParam[]>([
    { name: "query", type: DEFAULT_PARAM_TYPE, required: true, description: "" },
  ]);

  function addParam() {
    setParams((current) => [
      ...current,
      { name: "", type: DEFAULT_PARAM_TYPE, required: true, description: "" },
    ]);
  }

  function updateParam(index: number, patch: Partial<WorkflowDraftParam>) {
    setParams((current) =>
      current.map((item, itemIndex) => (itemIndex === index ? { ...item, ...patch } : item)),
    );
  }

  function removeParam(index: number) {
    setParams((current) => current.filter((_, i) => i !== index));
  }

  const paramNames = params.map((param) => param.name.trim()).filter(Boolean);
  const duplicateParams = new Set(paramNames).size !== paramNames.length;
  const invalidParam = params.some((param) => !param.name.trim());

  return { params, setParams, addParam, updateParam, removeParam, duplicateParams, invalidParam };
}

export type AuthorParamsState = ReturnType<typeof useAuthorParams>;
