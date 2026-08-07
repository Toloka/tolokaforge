"""What a well-formed ``state_checks.id_fields`` declaration looks like.

A table's declared key is one field name or an ordered list of component
names; a one-element list means exactly what the bare string means. Shared by
the authoring-side ``StateChecksConfig`` and the wire-side
``RunnerStateChecksConfig`` so both substrates refuse the same shapes with the
same messages.

This is the authoring gate only. Resolution semantics — including the
truthiness fallthrough where an *absent* table defaults to ``"id"`` — live in
``tolokaforge.runner.id_resolution``; that module tolerates shapes this one
refuses because it must also serve configs built before this gate existed.
"""

from __future__ import annotations


def validate_id_fields_declaration(
    value: dict[str, str | list[str]],
) -> dict[str, str | list[str]]:
    """Refuse an ``id_fields`` entry that cannot name its table's key."""
    for table, declared in value.items():
        if not (isinstance(table, str) and table.strip()):
            raise ValueError(f"state_checks.id_fields has a blank table name: {table!r}")
        if isinstance(declared, list):
            _validate_component_list(table, declared)
        elif not (isinstance(declared, str) and declared.strip()):
            raise ValueError(
                f"state_checks.id_fields[{table!r}] must be a non-empty key field, got {declared!r}"
            )
    return value


def _validate_component_list(table: str, components: list[str]) -> None:
    if not components:
        raise ValueError(
            f"state_checks.id_fields[{table!r}] declares an empty key field list — "
            f"name at least one field, e.g. [account_id] or [account_id, symbol]"
        )
    seen: set[str] = set()
    for component in components:
        if not (isinstance(component, str) and component.strip()):
            raise ValueError(
                f"state_checks.id_fields[{table!r}] has a component that is not a "
                f"non-empty key field: {component!r}"
            )
        if component in seen:
            raise ValueError(
                f"state_checks.id_fields[{table!r}] declares component {component!r} "
                f"twice — each key field may appear once"
            )
        seen.add(component)
