"""Config sub-package: wire DTOs and pure RouterConfig↔DTO mappers.

Extracted from config_state.py (god-file decomposition, Wave 1). Clean layering:
`dtos` (leaf) ← `mappers` ← `config_state` (the stateful orchestrator).
"""
