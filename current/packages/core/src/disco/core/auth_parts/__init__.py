"""Private implementation parts extracted from :mod:`disco.core.auth`.

This subpackage holds cohesive private authority (the local Preview gateway
listener pool, the CORS/same-host origin policy, and Preview origin hostname
validation) that previously lived in the single ``auth.py`` module. Nothing
here is part of the public API: ``auth.py`` remains the sole public-facing
module and re-imports every name defined here unchanged. External callers
must never import from ``auth_parts`` directly.
"""
