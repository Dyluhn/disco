"""Fixed-shape access policy for AppKit's persistent records primitive.

This is deliberately not a general authorization DSL.  It names only the
operations the trusted records generator can lower and verify end to end.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated

from pydantic import ConfigDict, Field, StringConstraints, field_validator
from pydantic.main import BaseModel

if TYPE_CHECKING:
    from ..spec import AppSpec, Entity

_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_IdStr = Annotated[str, StringConstraints(max_length=64)]
_STRICT = ConfigDict(extra="forbid", frozen=True)


class RecordPolicy(BaseModel):
    """Server-enforced policy for one records entity.

    ``owner_managed`` adds an implicit, server-derived ``owner_user_id`` and
    lets that owner update/delete the row. ``manage_roles`` may update/delete
    every row. ``lock_roles`` adds implicit ``locked`` state; those roles alone
    may lock or unlock. ``parent_lock_field`` names a declared
    foreign-key field whose lock state is checked before child creation/update.
    """

    model_config = _STRICT

    public_read: bool = False
    create_roles: tuple[_IdStr, ...] = Field(min_length=1)
    owner_managed: bool = False
    manage_roles: tuple[_IdStr, ...] = ()
    lock_roles: tuple[_IdStr, ...] = ()
    parent_lock_field: _IdStr | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @field_validator("create_roles", "manage_roles", "lock_roles")
    @classmethod
    def _roles_are_unique_identifiers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("record policy roles must be unique")
        for role in value:
            if _IDENT_RE.fullmatch(role) is None:
                raise ValueError("record policy roles must be snake_case identifiers")
        return value

    @field_validator("parent_lock_field")
    @classmethod
    def _parent_field_is_identifier(cls, value: str | None) -> str | None:
        if value is not None and _IDENT_RE.fullmatch(value) is None:
            raise ValueError("parent_lock_field must be a snake_case identifier")
        return value


def _validate_record_managed_fields(entity: Entity) -> None:
    if entity.record_policy is None:
        return
    collision = {"owner_user_id", "locked"}.intersection(field.name for field in entity.fields)
    if collision:
        raise ValueError(
            f"entity {entity.id!r} record_policy owns implicit fields "
            f"{sorted(collision)}; remove them from fields"
        )


def _validate_record_policy_roles(app: AppSpec) -> None:
    policy_enabled = bool(
        app.role_admin_roles or any(entity.record_policy is not None for entity in app.entities)
    )
    if policy_enabled and app.app_kind != "records":
        raise ValueError("record_policy and role_admin_roles require app_kind='records'")
    declared = set(app.roles)
    for role in app.role_admin_roles:
        if role not in declared:
            raise ValueError(
                f"role_admin_roles declares unknown role {role!r}; known roles: {sorted(declared)}"
            )
    for entity in app.entities:
        policy = entity.record_policy
        if policy is None:
            continue
        if not declared:
            raise ValueError(
                f"entity {entity.id!r} declares record_policy but AppSpec.roles is empty"
            )
        for role in (*policy.create_roles, *policy.manage_roles, *policy.lock_roles):
            if role not in declared:
                raise ValueError(
                    f"entity {entity.id!r} record_policy declares unknown role {role!r}; "
                    f"known roles: {sorted(declared)}"
                )


def _validate_record_parent_locks(app: AppSpec) -> None:
    entities = {entity.id: entity for entity in app.entities}
    for entity in app.entities:
        policy = entity.record_policy
        if policy is None or policy.parent_lock_field is None:
            continue
        field = next(
            (field for field in entity.fields if field.name == policy.parent_lock_field),
            None,
        )
        if field is None or field.references is None:
            raise ValueError(
                f"entity {entity.id!r} parent_lock_field "
                f"{policy.parent_lock_field!r} must name a foreign-key field"
            )
        parent = entities[field.references]
        if parent.record_policy is None or not parent.record_policy.lock_roles:
            raise ValueError(
                f"entity {entity.id!r} parent_lock_field references non-lockable "
                f"entity {parent.id!r}"
            )


__all__ = ["RecordPolicy"]
