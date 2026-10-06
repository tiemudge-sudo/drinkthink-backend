"""Canonical ingredient-ID resolution for DrinkThink runtime and migrations.

An active ``ingredient_id_merges`` row redirects its ``from_id`` to its
already-materialized ``resolved_to_id``. This module deliberately does not
follow ``to_id`` at runtime: the stored resolved target is the contract.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Set


class IngredientResolutionError(ValueError):
    """Raised when a value cannot resolve to an active canonical ingredient."""


_INTEGER_TEXT = re.compile(r"^[+-]?\d+$")


def parse_ingredient_id(value: object) -> int:
    """Parse one canonical integer ID without accepting fractional values."""
    if isinstance(value, bool):
        raise IngredientResolutionError("ingredient ID must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _INTEGER_TEXT.fullmatch(value.strip()):
        return int(value)
    raise IngredientResolutionError("ingredient ID must be an integer")


async def resolve_ingredient_id_map(
    database,
    values: Iterable[object],
    *,
    require_active: bool = True,
    strict: bool = True,
    active_merge_map: Mapping[int, int] | None = None,
    active_ingredient_ids: Set[int] | None = None,
) -> dict[int, int]:
    """Resolve source IDs with one merge query and one ingredient query.

    ``strict=False`` is for historical database reads: invalid or
    non-canonical values are omitted instead of failing a consumer read.
    New API writes use the default strict mode.
    """
    parsed: list[int] = []
    invalid = False
    for value in values:
        try:
            parsed.append(parse_ingredient_id(value))
        except IngredientResolutionError:
            invalid = True

    if invalid and strict:
        raise IngredientResolutionError("ingredient IDs must be canonical integers")

    source_ids = list(dict.fromkeys(parsed))
    if not source_ids:
        return {}

    redirects: dict[int, int] = {}
    if active_merge_map is not None:
        redirects = {
            source_id: active_merge_map[source_id]
            for source_id in source_ids
            if source_id in active_merge_map
        }
    else:
        merge_rows = await database.ingredient_id_merges.find(
            {"from_id": {"$in": source_ids}, "status": "active"},
            {"_id": 0, "from_id": 1, "resolved_to_id": 1},
        ).to_list(length=None)
        for row in merge_rows:
            try:
                redirects[parse_ingredient_id(row.get("from_id"))] = parse_ingredient_id(
                    row.get("resolved_to_id")
                )
            except IngredientResolutionError:
                if strict:
                    raise IngredientResolutionError("active ingredient merge contains a non-integer ID")

    resolved_ids = list(dict.fromkeys(redirects.get(source_id, source_id) for source_id in source_ids))
    if not require_active:
        return {source_id: redirects.get(source_id, source_id) for source_id in source_ids}

    if active_ingredient_ids is not None:
        active_ids = set(active_ingredient_ids)
    else:
        active_rows = await database.ingredients.find(
            {"ingredient_id": {"$in": resolved_ids}, "status": "active"},
            {"_id": 0, "ingredient_id": 1},
        ).to_list(length=None)
        active_ids = {parse_ingredient_id(row.get("ingredient_id")) for row in active_rows}
    unresolved = [source_id for source_id in source_ids if redirects.get(source_id, source_id) not in active_ids]
    if unresolved and strict:
        raise IngredientResolutionError(
            "unknown or inactive canonical ingredient IDs: " + ", ".join(map(str, unresolved))
        )
    return {
        source_id: canonical_id
        for source_id in source_ids
        if (canonical_id := redirects.get(source_id, source_id)) in active_ids
    }


async def resolve_ingredient_ids(
    database,
    values: Iterable[object],
    *,
    require_active: bool = True,
    strict: bool = True,
    active_merge_map: Mapping[int, int] | None = None,
    active_ingredient_ids: Set[int] | None = None,
) -> list[int]:
    """Resolve an ID sequence, preserving first occurrence after deduplication."""
    raw_values = list(values)
    redirects = await resolve_ingredient_id_map(
        database, raw_values, require_active=require_active, strict=strict,
        active_merge_map=active_merge_map, active_ingredient_ids=active_ingredient_ids,
    )
    resolved: list[int] = []
    seen: set[int] = set()
    for value in raw_values:
        try:
            source_id = parse_ingredient_id(value)
        except IngredientResolutionError:
            continue
        canonical_id = redirects.get(source_id)
        if canonical_id is not None and canonical_id not in seen:
            seen.add(canonical_id)
            resolved.append(canonical_id)
    return resolved


async def resolve_ingredient_id(database, value: object, *, require_active: bool = True) -> int:
    """Resolve one ID or raise ``IngredientResolutionError``."""
    resolved = await resolve_ingredient_ids(
        database, [value], require_active=require_active, strict=True
    )
    if not resolved:
        raise IngredientResolutionError("unknown or inactive canonical ingredient ID")
    return resolved[0]
