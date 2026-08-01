"""Lead-entity resolution — the P0 "which entity does this app persist" logic.

Split out of `..generator` (verbatim) to keep that module under the
`python_or_harness_module_logical_gt_700` budget. See `generator_parts/__init__.py`.
"""

from __future__ import annotations

from ..spec import AppSpec, Entity, EntityField

# ---- lead entity --------------------------------------------------------------

# The synthesized default lead entity when the AppSpec declares none — the P0
# minimum a lead-gen app needs to capture a contact.
_DEFAULT_LEAD_ID = "lead"
_DEFAULT_LEAD_FIELDS: tuple[tuple[str, str, bool], ...] = (
    ("name", "str", True),
    ("email", "str", True),
    ("message", "text", False),
)


def synthesized_lead_entity() -> Entity:
    """The sensible default lead entity (name/email/message) used when the AppSpec
    declares none. Deterministic — a fixed shape so the generated tree is stable."""
    return Entity(
        id=_DEFAULT_LEAD_ID,
        name="Lead",
        fields=tuple(EntityField(name=n, type=t, required=r) for n, t, r in _DEFAULT_LEAD_FIELDS),
    )


def _form_target_ids(app_spec: AppSpec) -> frozenset[str]:
    """Entity ids targeted by a `form` section's `content_ref` — the F3.1 form
    primitive's fold marker. Such entities are FORM-SUBMISSION planes, never lead
    candidates: without this skip, a folded form's `submit` action could flip
    `resolve_lead_entity` onto the form entity and silently re-shape the whole
    lead data plane. Empty for every pre-F3.1 spec (no form section sets
    `content_ref`), so lead resolution there is byte-for-byte unchanged."""
    return frozenset(
        section.content_ref
        for page in app_spec.pages
        for section in page.sections
        if section.kind == "form" and section.content_ref is not None
    )


def ensure_lead_entity(app_spec: AppSpec) -> AppSpec:
    """Return an AppSpec GUARANTEED to declare the lead entity it persists.

    If the spec already resolves a lead entity (a non-form `submit` target or an
    entity id `lead`) it is returned unchanged; otherwise the synthesized
    name/email/message entity is appended and the whole spec is re-validated.
    `app_create` calls this so the on-disk `appspec.json` always contains the
    entity the generated schema.sql / worker target — spec ⇄ tree never disagree
    about the lead shape. Form-entity submit targets (`_form_target_ids`) never
    satisfy the lead requirement."""
    by_id = {e.id: e for e in app_spec.entities}
    form_targets = _form_target_ids(app_spec)
    submit_targets = [a.target.strip() for a in app_spec.primary_actions if a.type == "submit"]
    if (
        any(t in by_id and t not in form_targets for t in submit_targets)
        or _DEFAULT_LEAD_ID in by_id
    ):
        return app_spec
    data = app_spec.model_dump(mode="json")
    data["entities"] = [*data["entities"], synthesized_lead_entity().model_dump(mode="json")]
    return AppSpec.model_validate(data)


def resolve_lead_entity(app_spec: AppSpec) -> Entity:
    """The ONE lead entity the app persists. P0 derivation, in order:

    1. the entity targeted by a `submit` primary action — SKIPPING form-submission
       entities (`_form_target_ids`), which own their own POST plane;
    2. an entity whose id is `lead`;
    3. otherwise the synthesized name/email/message default.

    Pure + deterministic — no spec mutation; `app_create` is what writes a
    synthesized entity back into the persisted AppSpec so spec ⇄ tree stay in sync."""
    submit_targets = [a.target.strip() for a in app_spec.primary_actions if a.type == "submit"]
    by_id = {e.id: e for e in app_spec.entities}
    form_targets = _form_target_ids(app_spec)
    for target in submit_targets:
        if target in form_targets:
            continue
        if target in by_id:
            return by_id[target]
    if _DEFAULT_LEAD_ID in by_id:
        return by_id[_DEFAULT_LEAD_ID]
    return synthesized_lead_entity()
