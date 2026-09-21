"""Packing audit over a forest of containers holding scanned items.

The request carries a list of containers, each with a unique
``container_id``, an optional ``parent_id`` (the container it is loaded
into), an optional ``max_weight`` (in ``target_unit``) and the ``items``
loaded directly into it.  Every item scan is fed through
:func:`app.gs1.parse` and must then contain:

* exactly one AI 01 (GTIN),
* exactly one AI 21 (serial number),
* exactly one weight element of the 310n (kg) / 320n (lb) family.

AI 10/17 elements are ignored when present.  Any parser failure or
missing/duplicated required AI fails the whole request with
:class:`PackingAuditError` whose ``detail`` names the offending
``container_index`` and ``item_index`` -- the same 422 shape as
``/weight-summary`` & friends.

The container hierarchy is validated before any scan is parsed, and
every violation rejects the whole request with
:class:`PackingStructureError` naming the offending ``container_index``:

* ``DUPLICATE_CONTAINER`` -- container ids must be unique; the detail
  also carries ``first_index`` (the container first using the id).
* ``UNKNOWN_PARENT`` -- ``parent_id`` must name another container of
  the request (a container may of course be a root).
* ``CYCLE_DETECTED`` -- following ``parent_id`` links must never loop
  (a container naming itself is a cycle of length one); the detail also
  carries ``cycle``, the container indices forming the loop.

Physical items are identified by (GTIN, serial); loading the same item
twice -- in one container or across containers -- rejects the whole
request with ``DUPLICATE_ITEM``, the detail locating both the current
and the first loading by container and item index.

Weights are rebuilt with :class:`decimal.Decimal` from the original six
digits and converted into ``target_unit`` with the fixed factors of
:mod:`app.summary`.  Aggregation runs from the leaves to the roots:
every container's ``total_weight`` is its own items plus the totals of
its child containers, and ``total_item_count`` counts the items
recursively.  A container whose ``total_weight`` exceeds its
``max_weight`` (compared on the exact, unrounded values) is flagged
``over_limit`` and its ``remaining_capacity`` goes negative -- the
audit still returns the complete result.  Rounding to six places
happens only when the response is rendered.
"""

from __future__ import annotations

from decimal import Decimal, localcontext

from .gs1 import GS1ParseError, Element, decode_weight, parse
from .summary import (
    ARITHMETIC_PRECISION,
    ScanValidationError,
    _to_target,
)

REQUIRED_AI_TEXT = (
    "exactly one AI 01 (GTIN), exactly one AI 21 (serial number), "
    "and exactly one weight AI 310n/320n"
)


class PackingAuditError(Exception):
    """A scan failed to parse or violated the per-scan AI shape.

    ``detail`` is the complete 422 payload: ``container_index`` and
    ``item_index`` plus the original parser/validation error under
    ``error``.
    """

    def __init__(self, detail: dict):
        super().__init__(detail["error"]["message"])
        self.detail = detail


class PackingStructureError(Exception):
    """The container hierarchy or the item placement is invalid.

    ``detail`` is the complete 422 payload: ``container_index`` (plus
    ``first_index`` for a duplicate id, ``cycle`` for a parent loop, or
    the item location fields for a duplicate loading) plus the
    structural error under ``error``.
    """

    def __init__(self, detail: dict):
        super().__init__(detail["error"]["message"])
        self.detail = detail


def _extract(
    where: str, elements: list[Element]
) -> tuple[str, str, Decimal, str]:
    """Pull (GTIN, serial, exact weight, weight unit) from one scan."""
    gtins = [e for e in elements if e.ai == "01"]
    serials = [e for e in elements if e.ai == "21"]
    weights = [e for e in elements if e.unit is not None]

    if len(gtins) != 1:
        if not gtins:
            raise ScanValidationError(
                "MISSING_AI",
                f"{where}: missing required AI 01 (GTIN)",
                expected=REQUIRED_AI_TEXT,
            )
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"{where}: AI 01 (GTIN) appears {len(gtins)} times; exactly one is required",
            expected="exactly one AI 01 (GTIN)",
            found=f"{len(gtins)} AI 01 elements",
            offset=gtins[1].offset,
        )

    if len(serials) != 1:
        if not serials:
            raise ScanValidationError(
                "MISSING_AI",
                f"{where}: missing required AI 21 (serial number)",
                expected=REQUIRED_AI_TEXT,
            )
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"{where}: AI 21 (serial number) appears {len(serials)} times; exactly one is required",
            expected="exactly one AI 21 (serial number)",
            found=f"{len(serials)} AI 21 elements",
            offset=serials[1].offset,
        )

    if len(weights) != 1:
        if not weights:
            raise ScanValidationError(
                "MISSING_AI",
                f"{where}: missing required weight AI 310n/320n",
                expected=REQUIRED_AI_TEXT,
            )
        ais = ", ".join(e.ai for e in weights)
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"{where}: {len(weights)} weight elements ({ais}); "
            "exactly one 310n/320n is required",
            expected="exactly one weight AI 310n (kg) or 320n (lb)",
            found=ais,
            offset=weights[1].offset,
        )

    weight = weights[0]
    value = decode_weight(weight.value, int(weight.ai[3]))
    return gtins[0].value, serials[0].value, value, weight.unit


def _scan(
    container_index: int, item_index: int, scan: str
) -> tuple[str, str, Decimal, str]:
    """Parse and validate one item scan, locating it in the 422 detail."""
    where = f"container {container_index} item {item_index}"
    try:
        elements = parse(scan)
    except GS1ParseError as exc:
        detail = {
            "container_index": container_index,
            "item_index": item_index,
            "error": exc.to_dict(),
        }
        raise PackingAuditError(detail) from exc
    try:
        return _extract(where, elements)
    except ScanValidationError as exc:
        detail = {
            "container_index": container_index,
            "item_index": item_index,
            "error": exc.to_dict(),
        }
        raise PackingAuditError(detail) from exc


def _check_structure(
    ids: list[str], parent_ids: list[str | None]
) -> list[int | None]:
    """Validate the container hierarchy and resolve parents to indices.

    Returns ``parent`` where ``parent[i]`` is the parent container's
    request index (``None`` for roots).  Raises
    :class:`PackingStructureError` on a duplicate id, an unknown parent
    or a parent cycle.
    """
    index_of: dict[str, int] = {}
    for index, container_id in enumerate(ids):
        first = index_of.get(container_id)
        if first is not None:
            raise PackingStructureError(
                {
                    "container_index": index,
                    "first_index": first,
                    "error": {
                        "error": "DUPLICATE_CONTAINER",
                        "message": (
                            f"container {index}: container_id '{container_id}' "
                            f"is already used by container {first}; container "
                            "ids must be unique"
                        ),
                        "expected": "unique container_id across all containers",
                        "found": container_id,
                    },
                }
            )
        index_of[container_id] = index

    for index, parent_id in enumerate(parent_ids):
        if parent_id is not None and parent_id not in index_of:
            raise PackingStructureError(
                {
                    "container_index": index,
                    "error": {
                        "error": "UNKNOWN_PARENT",
                        "message": (
                            f"container {index}: parent_id '{parent_id}' does "
                            "not match any container_id in the request"
                        ),
                        "expected": (
                            "parent_id of an existing container "
                            "(or null for a root)"
                        ),
                        "found": parent_id,
                    },
                }
            )

    parent: list[int | None] = [
        index_of[parent_id] if parent_id is not None else None
        for parent_id in parent_ids
    ]
    cycle = _find_cycle(parent)
    if cycle is not None:
        chain = " -> ".join(str(c) for c in (*cycle, cycle[0]))
        raise PackingStructureError(
            {
                "container_index": cycle[0],
                "cycle": cycle,
                "error": {
                    "error": "CYCLE_DETECTED",
                    "message": (
                        f"containers {chain} form a parent/child cycle; the "
                        "container hierarchy must be a forest"
                    ),
                    "expected": (
                        "an acyclic parent/child container hierarchy (a forest)"
                    ),
                    "found": f"cycle {chain}",
                },
            }
        )
    return parent


def _find_cycle(parent: list[int | None]) -> list[int] | None:
    """One parent-pointer cycle, containers scanned in request order.

    Deterministic: the walk starts at the lowest unvisited index and
    follows parent links, so the first repeated node of the current walk
    opens the returned cycle.  Returns ``None`` for a forest.
    """
    visited = [False] * len(parent)
    for start in range(len(parent)):
        if visited[start]:
            continue
        path: list[int] = []
        position: dict[int, int] = {}
        node: int | None = start
        while node is not None and not visited[node]:
            position[node] = len(path)
            path.append(node)
            visited[node] = True
            node = parent[node]
        if node is not None and node in position:
            return path[position[node] :]
    return None


def _post_order(
    parent: list[int | None], children: list[list[int]]
) -> list[int]:
    """Container indices with every child ahead of its parent."""
    order: list[int] = []
    for root in range(len(parent)):
        if parent[root] is not None:
            continue
        stack = [(root, False)]
        while stack:
            node, expanded = stack.pop()
            if expanded:
                order.append(node)
                continue
            stack.append((node, True))
            for child in children[node]:
                stack.append((child, False))
    return order


def audit_packing(
    containers: list[tuple[str, str | None, Decimal | None, list[str]]],
    target_unit: str,
) -> dict:
    """Validate the container forest and aggregate weights leaf to root.

    ``containers`` is a list of ``(container_id, parent_id, max_weight,
    items)`` 4-tuples in request order, ``max_weight`` expressed in
    ``target_unit`` (``None`` = no limit).  Returns ``{"target_unit":
    ..., "containers": [...]}`` with one entry per container in request
    order, holding the echoed identifiers, ``max_weight``, the
    ``direct_weight`` / ``total_weight`` (both unrounded
    :class:`Decimal` in the target unit), ``remaining_capacity``
    (``None`` when the container has no limit), the ``over_limit`` flag
    and the ``direct_item_count`` / ``total_item_count``.
    """
    ids = [container_id for container_id, _, _, _ in containers]
    parent_ids = [parent_id for _, parent_id, _, _ in containers]
    parent = _check_structure(ids, parent_ids)

    with localcontext() as ctx:
        ctx.prec = ARITHMETIC_PRECISION

        direct_weight: list[Decimal] = []
        loaded: dict[tuple[str, str], tuple[int, int]] = {}
        for container_index, (_, _, _, items) in enumerate(containers):
            total = Decimal(0)
            for item_index, scan in enumerate(items):
                gtin, serial, weight, unit = _scan(
                    container_index, item_index, scan
                )
                key = (gtin, serial)
                first = loaded.get(key)
                if first is not None:
                    first_container, first_item = first
                    raise PackingStructureError(
                        {
                            "container_index": container_index,
                            "item_index": item_index,
                            "first_container_index": first_container,
                            "first_item_index": first_item,
                            "error": {
                                "error": "DUPLICATE_ITEM",
                                "message": (
                                    f"container {container_index} item "
                                    f"{item_index}: item GTIN '{gtin}' serial "
                                    f"'{serial}' is already loaded in "
                                    f"container {first_container} (item "
                                    f"{first_item}); each physical item may "
                                    "be loaded only once"
                                ),
                                "expected": (
                                    f"item loaded at most once (already in "
                                    f"container {first_container} item "
                                    f"{first_item})"
                                ),
                                "found": f"GTIN '{gtin}' serial '{serial}'",
                            },
                        }
                    )
                loaded[key] = (container_index, item_index)
                total += _to_target(weight, unit, target_unit)
            direct_weight.append(total)

        children: list[list[int]] = [[] for _ in containers]
        for index, parent_index in enumerate(parent):
            if parent_index is not None:
                children[parent_index].append(index)

        # Leaf-to-root: every child is totalled before its parent.
        total_weight = [Decimal(0)] * len(containers)
        total_count = [0] * len(containers)
        for node in _post_order(parent, children):
            total = direct_weight[node]
            count = len(containers[node][3])
            for child in children[node]:
                total += total_weight[child]
                count += total_count[child]
            total_weight[node] = total
            total_count[node] = count

        results = []
        for index, (container_id, parent_id, max_weight, items) in enumerate(
            containers
        ):
            total = total_weight[index]
            remaining = (
                max_weight - total if max_weight is not None else None
            )
            results.append(
                {
                    "container_index": index,
                    "container_id": container_id,
                    "parent_id": parent_id,
                    "max_weight": max_weight,
                    "direct_weight": direct_weight[index],
                    "total_weight": total,
                    "remaining_capacity": remaining,
                    "over_limit": (
                        max_weight is not None and total > max_weight
                    ),
                    "direct_item_count": len(items),
                    "total_item_count": total_count[index],
                }
            )

    return {"target_unit": target_unit, "containers": results}
