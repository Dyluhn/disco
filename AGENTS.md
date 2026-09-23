# Provider-neutral LLM integration

Owner instruction (2026-09-22): "NEVER BUILD MODEL/LLM PROVIDER SPECIFIC ITEMS INTO THE CODE."

Do not select runtime behavior by matching an LLM model name, provider label, or vendor hostname. Implement capabilities and protocol options through explicit, validated configuration and shared paths. Do not move a name-based registry into another file and call that provider neutrality. Test arbitrary identifiers and endpoints, including negative controls proving familiar names gain no implicit behavior. Real captured fixtures may identify their source; that provenance must not control runtime behavior.
