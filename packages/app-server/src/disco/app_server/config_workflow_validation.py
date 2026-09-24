"""Validate a custom image graph before changing persisted configuration."""

from __future__ import annotations

import json
import re

from .config_state_errors import ConfigValidationError


def validate_image_workflow(value: str) -> None:
    if not value.strip():
        return
    detail = "Workflow must be a non-empty JSON object of nodes."
    if re.search(r'"\s*%(seed|width|height)%\s*"', value):
        detail = "Numeric workflow tokens (%seed%, %width%, %height%) must be unquoted."
    else:
        probe = re.sub(r"%(seed|width|height)%", "0", value)
        probe = re.sub(r"%(prompt|negative|ckpt)%", "x", probe)
        try:
            parsed = json.loads(probe)
            if isinstance(parsed, dict) and parsed:
                return
        except ValueError:
            detail = "Workflow is not valid JSON. Paste a Save (API Format) export."
    raise ConfigValidationError("invalid_image_workflow", detail=detail)
