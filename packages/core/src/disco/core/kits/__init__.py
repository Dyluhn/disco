"""disco.core.kits — host-owned Starter + Brand kits (P7).

Starters resolve a contract's ``starter_kit`` name to a real host-owned scaffold so the
model edits a frame instead of hand-drawing chrome. The brand kit projects the existing
disco.core.brand themes into AppKit's token vocabulary (no parallel catalog).
"""

from __future__ import annotations

from .brand import BRAND_NAMES, appkit_brand, brand_to_appkit_tokens
from .starter import StarterKit, StarterKitRegistry, lead_form_appspec

__all__ = [
    "BRAND_NAMES",
    "StarterKit",
    "StarterKitRegistry",
    "appkit_brand",
    "brand_to_appkit_tokens",
    "lead_form_appspec",
]
