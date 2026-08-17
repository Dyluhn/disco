"""Shared config, regex patterns, and bounded-string caps for the release spec.

Extracted from ``spec.py`` to reduce module size; the public facade re-imports
these names unchanged. Pure constants/type aliases — no dependency on the spec
models themselves, so this is a leaf module within ``spec_parts``.
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import ConfigDict, StringConstraints

# ---- shared config ------------------------------------------------------------
#
# Reject unknown fields everywhere (a stray key is a typo / version skew we want
# to fail loudly), and freeze every model. `frozen=True` blocks attribute
# REASSIGNMENT; every collection field is a `tuple[...]`, not a `list[...]`, so an
# element cannot be appended / replaced in place to smuggle a duplicate id or a
# broken command past the cross-field validators and on to serialization. A
# validated ReleaseSpec is therefore immutable end to end — see the AppKit spec
# (`disco.core.appkit.spec`) this deliberately mirrors.
#
# `hide_input_in_errors=True` keeps pydantic from appending `input_value=...` to a
# validation error message. These models are NAMES-ONLY value guards: a rejected
# argv token (`API_TOKEN=hunter2`) or env NAME could itself carry a secret VALUE,
# so the raw error must never echo the offending input — a `str(ValidationError)`
# names the FIELD, never the value.
_STRICT = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

# An environment variable NAME: UPPERCASE env-var style. A leading letter or
# underscore, then letters / digits / underscores. This rejects the exact classes
# the acceptance criteria call out — an '=', any whitespace, lower-case, a leading
# digit, or any other punctuation — so a declared name is always a safe shell /
# compose identifier.
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

# A service / resource id (and the ids that reference them): snake/kebab-case,
# starting with a lowercase letter — a safe compose service name and dependency
# key. Letters, digits, underscore, hyphen.
_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

# A local volume name (compose/docker style): starts alphanumeric, then a small
# safe charset.
_VOLUME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")

# `tree_digest` mirrors `VersionRecord.tree_digest`, which the project store emits
# as a BARE lowercase sha256 hexdigest (64 hex chars, no algorithm prefix). Pin
# the exact shape so a spec can only bind to a real store digest.
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

# ---- bounded string / collection caps -----------------------------------------
#
# Every free-form string is length-capped and every list is count-capped so a
# schema-valid spec is bounded in size (validation cost + serialized bytes) and a
# hostile/corrupt spec can't become a memory bomb — the same discipline the AppKit
# specs use.
_ID_MAX = 64
_NAME_MAX = 200
_SHORT_MAX = 120
_PATH_MAX = 512
_URL_MAX = 2048
_ENV_NAME_MAX = 128
_ARGV_ITEM_MAX = 2048
_REASON_MAX = 2000
_DIGEST_MAX = 64

_MAX_SERVICES = 50
_MAX_ENV = 300
_MAX_RESOURCES = 50
_MAX_ARGV = 200
_MAX_LINKS = 50  # depends_on / consumers fan-out
_MAX_TARGETS = 20
_MAX_GENERATED_FILES = 500
_MAX_EVIDENCE = 200

_IdStr = Annotated[str, StringConstraints(max_length=_ID_MAX)]
_NameStr = Annotated[str, StringConstraints(max_length=_NAME_MAX)]
_ShortStr = Annotated[str, StringConstraints(max_length=_SHORT_MAX)]
_PathStr = Annotated[str, StringConstraints(max_length=_PATH_MAX)]
_UrlStr = Annotated[str, StringConstraints(max_length=_URL_MAX)]
_EnvNameStr = Annotated[str, StringConstraints(max_length=_ENV_NAME_MAX)]
_ArgvItemStr = Annotated[str, StringConstraints(max_length=_ARGV_ITEM_MAX)]
_ReasonStr = Annotated[str, StringConstraints(max_length=_REASON_MAX)]
_DigestStr = Annotated[str, StringConstraints(max_length=_DIGEST_MAX)]
