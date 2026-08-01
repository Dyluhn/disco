"""Private implementation parts extracted from :mod:`disco.tools.builtin._slides_pipeline`.

This subpackage holds the cohesive private helpers that previously lived in the
single ``_slides_pipeline.py`` module. Nothing here is part of the public API:
``_slides_pipeline.py`` remains the sole public compatibility/export facade and
re-imports these names at module level. External callers (including
``find_and_edit.py``, which imports ``_extract_json_object``,
``_purpose_for_model_endpoint``, ``_resolve_llm_key`` and ``_resolve_slides_llm``)
must never import from ``_slides_pipeline_parts`` directly — go through
``disco.tools.builtin._slides_pipeline``.

Layering within this subpackage (no cycles):
  ``_types``       -- shared dataclasses/exceptions, no dependencies of its own.
  ``_constants``   -- the theme/archetype enum strings derived from
     ``disco.tools.builtin._deck_schema``.
  ``_prompts``     -- the system/user prompt templates. No dependencies.
  ``_llm_endpoint``-- ConfigStore-based LLM endpoint + secret resolution for the
     deck-author model. Deliberately does NOT import ``disco.agent_server`` (see
     its module docstring) — preserve that when touching it.
  ``_llm_client``  -- the raw chat-completions transport + response-completion
     check. Depends on ``_types`` and ``_constants``.
  ``_craft``       -- theme/craft defaults and density/image-slot
     post-processing. Depends on ``disco.tools.builtin._deck_schema`` only.
  ``_json_deck_parse`` -- JSON extraction + AuthoredDeck validation. Depends on
     ``_craft`` (theme-alias coercion, layout-hint nulling).

``disco.tools.builtin._slides_pipeline`` imports FROM these modules and
re-exports; none of these modules import back from the parent, so there is no
parent<->parts cycle to reason about here.
"""
