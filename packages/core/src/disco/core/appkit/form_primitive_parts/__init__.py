"""Private implementation parts extracted from :mod:`disco.core.appkit.form_primitive`.

This subpackage holds the cohesive private helpers (the declarative spec
models, the FormSpec -> AppSpec fold, the lowering into D1/Drizzle/Worker
surface, and the React form component emitter) that previously lived in the
single ``form_primitive.py`` module. Nothing here is part of the public API:
``form_primitive.py`` remains the sole public-facing module and re-imports
every name defined here. External callers must never import from
``form_primitive_parts`` directly.
"""
