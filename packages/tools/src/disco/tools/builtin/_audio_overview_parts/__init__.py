"""Private implementation parts extracted from :mod:`disco.tools.builtin.audio_overview`.

This subpackage holds the cohesive private helpers that previously lived in the
single ``audio_overview.py`` module. Nothing here is part of the public API:
``audio_overview.py`` remains the sole public compatibility/export facade and
re-imports these names at module level. External callers must never import from
``_audio_overview_parts`` directly — go through ``disco.tools.builtin.audio_overview``
(or ``disco.tools.builtin`` for the tool class itself).

Layering within this subpackage (no cycles):
  ``_types``    -- shared dataclasses/exceptions, no dependencies of its own.
  ``_payload``  -- turn-script LLM request construction. Depends on ``_types``.
  ``_llm_client`` -- OpenAI-compatible chat-completions transport. Depends on ``_types``.
  ``_validation`` -- turn-script JSON parsing/validation. Depends on ``_types``.
  ``_script_generation`` -- the bounded batch-assembly loop. Depends on
     ``_types``, ``_payload``, ``_validation``.
  ``_synthesis`` -- local/remote TTS synthesis (float32 PCM). Depends on nothing
     above other than the sibling ``_tts_normalize`` module.

``disco.tools.builtin.audio_overview`` imports FROM these modules and re-exports;
none of these modules import back from the parent, so there is no parent<->parts
cycle to reason about here (unlike some other ``_parts`` packages in this
campaign where a callback into the parent is genuinely needed).
"""
