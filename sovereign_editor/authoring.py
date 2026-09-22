"""Explicit authored map transactions: placement translation plus permission cells.

One transaction is one save/undo/export unit. It carries the exact before-values of
every byte it will change, a digest of the resources it depends on, and the index it
composes at, so a stale or reordered edit is refused rather than silently reapplied.

Everything that drives a byte patch is stored **resource-local**: the 16.16 record
words of a placement and the byte offset of a permission cell inside the map member.
Global tile anchors are display values derived from the matrix cell the author worked
in. A map member reused by several matrix cells therefore composes as one resource,
and no alternate cell origin can hide an overlapping byte.

Scope and honesty rules kept from the earlier milestones:

* Model bounds never identify collision ownership. Every permission cell in a
  transaction was selected by the author, and its before/after pair is recorded.
* A coordinate-only edit changes no permission byte, and says so.
* Placement height, BDHC terrain, BGS, doors, warps, triggers and scripts are not
  moved with a building; the record's Y word is copied through unchanged.
* An authored transaction is not an M3/M4/M5 qualification and never claims one.

Record layout comes from DSPRE ``DS_Map/ROMFiles/MapFile.cs`` (AGPL-3.0, pinned
commit 249a278186d80c35f04485b2c1b612bd81b90a74); see ``references/dspre``.
"""
import copy
import json
import math
import struct

from .formats import digest, require
from . import world

SCHEMA = "sovereign-map-transaction-v1"
VERSION = 1
# MapFile.cs building record: model id, then X/Y/Z as 16.16 words.
AXES = {"x": 4, "y": 8, "z": 12}
EDITABLE_AXES = ("x", "z")
HEIGHT_PRESERVED = ("Placement height is preserved in this milestone: the record's Y word and the "
                    "BDHC terrain are copied through unchanged. Edit X and Z only.")
NOT_A_QUALIFICATION = ("explicit authored edit; not an M3/M4/M5 qualification and not "
                       "native-accepted until the exported ROM is played")

CONTEXT_KEYS = {"id", "header", "matrix", "cell", "map_member", "origin"}
PLACEMENT_KEYS = {"slot", "model_id", "record_offset", "record_before_sha256", "before", "after",
                  "record_before", "record_after", "rotation_raw", "scale_raw", "unknown_hex"}
PERMISSION_KEYS = {"x", "z", "offset", "before", "after", "source"}
TRANSACTION_KEYS = {"schema", "version", "index", "label", "context", "placements", "permissions",
                    "dependencies", "dependencies_sha256", "qualification", "delta"}


def canonical(value):
    return digest(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def context_ref(context):
    return {"id": context["id"], "header": context["header"]["id"],
            "matrix": context["matrix"]["id"],
            "cell": [context["cell"]["x"], context["cell"]["y"]],
            "map_member": context["map_member"], "origin": list(context["origin"])}


def dependencies(context, index):
    """Everything this transaction was authored against, including its position."""
    return {"baseline_resource": {"archive": world.MAP_ARCHIVE, "member": context["map_member"],
                                  "sha256": context["map_sha256"], "bytes": context["map_bytes"]},
            "header_hex": context["header"]["hex"], "header_arm9_offset": context["header"]["arm9_offset"],
            "matrix_sha256": context["matrix"]["sha256"], "area_data_sha256": context["area_data"]["sha256"],
            "event_member": context["event_member"], "sections": context["sections"],
            "applies_after": index}


# ---- resource-local <-> global tile anchors ---------------------------------


def record_state(prop):
    """The placement's three 16.16 record words; the authoritative composed state."""
    return dict(zip(("x", "y", "z"), prop["xyz_raw"]))


def global_from_record(context, record):
    """Display anchor of a record inside one matrix cell. Never drives a patch."""
    ox, oz = context["origin"]
    half = world.MAP_SIZE // 2
    return {"x": ox + half + record["x"] / 65536, "y": record["y"] / 65536,
            "z": oz + half + record["z"] / 65536}


def global_position(context, prop):
    return global_from_record(context, record_state(prop))


def record_from_global(context, position, record):
    """Author's global X/Z anchor -> record words, with the stock Y word preserved."""
    ox, oz = context["origin"]
    half = world.MAP_SIZE // 2
    return {"x": world.record_value(position["x"] - ox - half), "y": record["y"],
            "z": world.record_value(position["z"] - oz - half)}


def require_anchor(context, position, what="Placement anchor"):
    ox, oz = context["origin"]
    for axis, origin in (("x", ox), ("z", oz)):
        value = position[axis]
        require(type(value) in (int, float) and math.isfinite(value),
                f"{what} {axis} must be a finite number", "UNQUALIFIED_TARGET")
        require(0 <= value - origin <= world.MAP_SIZE,
                f"{what} must stay inside map cell {context['cell']['x']},{context['cell']['y']} "
                f"({origin}..{origin + world.MAP_SIZE} on {axis})", "OUTSIDE_MAP")


def require_height_preserved(before, after):
    require(before["y"] == after["y"], HEIGHT_PRESERVED, "UNSUPPORTED_EDIT")


# ---- permission cells --------------------------------------------------------


def permission_pair(raw, offset, overrides):
    return overrides.get(offset, raw[offset:offset + 2])


def require_cell_request(cell):
    require(isinstance(cell, dict), "A permission edit must be an object with x, z and after")
    require(type(cell.get("x")) is int and type(cell.get("z")) is int,
            "Permission cells use integer global tile x/z")
    require(set(cell) <= {"x", "z", "after", "before", "source"},
            f"Unsupported permission field(s): {sorted(set(cell) - {'x', 'z', 'after', 'before', 'source'})}")
    for key in ("after", "before"):
        value = cell.get(key)
        if key == "after" or value is not None:
            require(isinstance(value, str) and len(value) == 4
                    and all(c in "0123456789abcdefABCDEF" for c in value),
                    f"Permission {key} must be a two-byte type/collision hex value such as 0080")


def translate_selection(context, raw, overrides, cells, delta):
    """Move the blocking flag of explicitly selected cells by a whole-tile delta.

    Overlapping source/destination cells resolve deterministically: a cell ends
    blocked exactly when it is a destination. Terrain type bytes never move.
    """
    dx, dz = delta
    require(type(dx) is int and type(dz) is int, "Collision selection moves in whole tiles",
            "UNQUALIFIED_TARGET")
    source = []
    for cell in cells:
        require_cell_request({**cell, "after": "0000"} if "after" not in cell else cell)
        x, z = cell["x"], cell["z"]
        offset = world.cell_offset(context, x, z)
        pair = permission_pair(raw, offset, overrides)
        require(pair[1] & 128, f"Selected cell {x},{z} is not blocked; select the cells you mean to move",
                "BEFORE_VALUE_MISMATCH")
        source.append((x, z))
    destination = {(x + dx, z + dz) for x, z in source}
    result = []
    for x, z in sorted(set(source) | destination):
        offset = world.cell_offset(context, x, z)
        pair = permission_pair(raw, offset, overrides)
        after = bytes((pair[0], (pair[1] & ~128) | (128 if (x, z) in destination else 0)))
        result.append({"x": x, "z": z, "after": after.hex(), "before": pair.hex()})
    return result


def merge_cells(explicit, translated):
    """Explicit selections and a translated selection must agree, or refuse.

    A before-value supplied by either side is kept; two different before-values for
    one cell are a conflict, never a silent choice.
    """
    merged = {}
    for source, cells in (("selected", explicit), ("moved-with-placement", translated)):
        for cell in cells:
            key = (cell["x"], cell["z"])
            previous = merged.get(key)
            if previous is None:
                merged[key] = {**cell, "source": source}
                continue
            require(previous["after"] == cell["after"],
                    f"Conflicting after-values for permission cell {key[0]},{key[1]}: "
                    f"{previous['after']} and {cell['after']}", "CELL_CONFLICT")
            before = previous.get("before")
            other = cell.get("before")
            require(before is None or other is None or before == other,
                    f"Conflicting before-values for permission cell {key[0]},{key[1]}: "
                    f"{before} and {other}", "CELL_CONFLICT")
            merged[key] = {**previous, "before": before if before is not None else other,
                           "source": previous["source"] + "+" + source}
    return [merged[key] for key in sorted(merged)]


# ---- building and replaying one transaction ---------------------------------


def build(context, raw, props, placement, cells, index, label, placement_state, permission_state):
    """Assemble one transaction against the composed current state. Never saves."""
    member = context["map_member"]
    placements, delta = [], None
    if placement is not None:
        require(isinstance(placement, dict) and "slot" in placement,
                "A placement request needs a slot and an X and/or Z target")
        require(set(placement) <= {"slot"} | set(AXES),
                f"Unsupported placement field(s): {sorted(set(placement) - {'slot'} - set(AXES))}")
        require("y" not in placement, HEIGHT_PRESERVED, "UNSUPPORTED_EDIT")
        slot = placement["slot"]
        require(type(slot) is int and 0 <= slot < len(props),
                f"No placement {member}:{slot} in this map context", "NOT_FOUND")
        prop = props[slot]
        record_before = placement_state.get((member, slot)) or record_state(prop)
        before = global_from_record(context, record_before)
        target = {axis: placement.get(axis, before[axis]) for axis in EDITABLE_AXES}
        require_anchor(context, target)
        record_after = record_from_global(context, target, record_before)
        require_height_preserved(record_before, record_after)
        after = global_from_record(context, record_after)
        if record_after != record_before:
            delta = (after["x"] - before["x"], after["z"] - before["z"])
            placements.append({"slot": slot, "model_id": prop["model_id"],
                               "record_offset": prop["record_offset"],
                               "record_before_sha256": prop["record_sha256"],
                               "before": before, "after": after,
                               "record_before": record_before, "record_after": record_after,
                               "rotation_raw": prop["rotation_raw"], "scale_raw": prop["scale_raw"],
                               "unknown_hex": prop["unknown_hex"]})
    permissions = []
    for cell in cells:
        require_cell_request(cell)
        x, z = cell["x"], cell["z"]
        offset = world.cell_offset(context, x, z)
        current = permission_state.get((member, offset), raw[offset:offset + 2])
        after = bytes.fromhex(cell["after"])
        if cell.get("before") is not None:
            require(cell["before"].lower() == current.hex(),
                    f"Permission cell {x},{z} is {current.hex()}, not {cell['before'].lower()}",
                    "BEFORE_VALUE_MISMATCH")
        if after == current:
            continue
        permissions.append({"x": x, "z": z, "offset": offset, "before": current.hex(),
                            "after": after.hex(), "source": cell.get("source", "selected")})
    deps = dependencies(context, index)
    return {"schema": SCHEMA, "version": VERSION, "index": index,
            "label": label or f"{context['name']} {member}: authored map edit",
            "context": context_ref(context), "placements": placements, "permissions": permissions,
            "dependencies": deps, "dependencies_sha256": canonical(deps),
            "qualification": NOT_A_QUALIFICATION,
            "delta": list(delta) if delta else None}


def require_shape(transaction):
    """Refuse a malformed or forged transaction before any state is derived from it."""
    extended = isinstance(transaction, dict) and transaction.get("version") == 2
    keys = TRANSACTION_KEYS | {"sign_interaction"} if extended else TRANSACTION_KEYS
    require(isinstance(transaction, dict) and set(transaction) == keys,
            "Unknown authored map transaction fields", "UNSUPPORTED_EDIT")
    require((transaction["schema"], transaction["version"]) in
            ((SCHEMA, VERSION), ("sovereign-map-transaction-v2", 2)),
            "Unknown authored map transaction version", "UNSUPPORTED_EDIT")
    if extended:
        require(isinstance(transaction["sign_interaction"], dict),
                "Invalid sign interaction change", "UNSUPPORTED_EDIT")
    require(type(transaction["index"]) is int and transaction["index"] >= 0,
            "Invalid transaction index", "UNSUPPORTED_EDIT")
    require(isinstance(transaction["label"], str) and isinstance(transaction["qualification"], str),
            "Invalid transaction label", "UNSUPPORTED_EDIT")
    require(isinstance(transaction["context"], dict) and set(transaction["context"]) == CONTEXT_KEYS,
            "Invalid authored transaction context", "UNSUPPORTED_EDIT")
    require(isinstance(transaction["placements"], list) and isinstance(transaction["permissions"], list),
            "Invalid transaction contents", "UNSUPPORTED_EDIT")
    for change in transaction["placements"]:
        require(isinstance(change, dict) and set(change) == PLACEMENT_KEYS,
                "Invalid authored placement record", "UNSUPPORTED_EDIT")
        for key in ("record_before", "record_after"):
            record = change[key]
            require(isinstance(record, dict) and set(record) == {"x", "y", "z"}
                    and all(type(v) is int and -(2 ** 31) <= v < 2 ** 31 for v in record.values()),
                    "Placement records are three 16.16 integers", "UNSUPPORTED_EDIT")
        require_height_preserved(change["record_before"], change["record_after"])
        require(type(change["record_offset"]) is int and change["record_offset"] >= 0,
                "Invalid placement record offset", "UNSUPPORTED_EDIT")
    for cell in transaction["permissions"]:
        require(isinstance(cell, dict) and set(cell) == PERMISSION_KEYS,
                "Invalid authored permission cell", "UNSUPPORTED_EDIT")
        require(type(cell["offset"]) is int and cell["offset"] >= 0, "Invalid permission offset", "UNSUPPORTED_EDIT")
        for key in ("before", "after"):
            require(isinstance(cell[key], str) and len(cell[key]) == 4
                    and all(c in "0123456789abcdef" for c in cell[key]),
                    "Permission values are two-byte lowercase hex", "UNSUPPORTED_EDIT")


def require_record_identity(change, prop, context):
    """The slot this transaction claims must still be the record it will patch."""
    require(change["record_offset"] == prop["record_offset"],
            f"Placement {context['map_member']}:{change['slot']} record moved to "
            f"{prop['record_offset']}; the transaction claims {change['record_offset']}",
            "BEFORE_VALUE_MISMATCH")
    require(change["record_before_sha256"] == prop["record_sha256"],
            "Placement record differs from the authored baseline", "BEFORE_VALUE_MISMATCH")
    require(change["model_id"] == prop["model_id"] and change["rotation_raw"] == prop["rotation_raw"]
            and change["scale_raw"] == prop["scale_raw"] and change["unknown_hex"] == prop["unknown_hex"],
            "Placement identity or preserved fields differ from the authored baseline",
            "BEFORE_VALUE_MISMATCH")
    sections = context["sections"]
    start = sections["buildings_offset"] + change["slot"] * sections["building_record_bytes"]
    require(change["record_offset"] == start,
            "Placement record offset is outside its slot in the buildings section", "BEFORE_VALUE_MISMATCH")


def require_cell_bounds(cell, context):
    sections = context["sections"]
    start = sections["permissions_offset"]
    require(start <= cell["offset"] <= start + sections["permissions_bytes"] - 2
            and (cell["offset"] - start) % 2 == 0,
            "Permission offset is outside the map's permission section", "BEFORE_VALUE_MISMATCH")
    require(cell["offset"] == world.cell_offset(context, cell["x"], cell["z"]),
            "Permission cell offset disagrees with its tile", "BEFORE_VALUE_MISMATCH")


def is_empty(transaction):
    return not transaction["placements"] and not transaction["permissions"] and not transaction.get("sign_interaction")


def patches(context, transaction):
    """Byte patches. Offsets are resource-local, so the matrix cell cannot shift them."""
    result = []
    base = context["map_rom_offset"]
    for change in transaction["placements"]:
        require_height_preserved(change["record_before"], change["record_after"])
        for axis in EDITABLE_AXES:
            before = struct.pack("<i", change["record_before"][axis])
            after = struct.pack("<i", change["record_after"][axis])
            if before != after:
                result.append({"kind": f"placement.{axis}", "map_member": context["map_member"],
                               "rom_offset": base + change["record_offset"] + AXES[axis],
                               "before": before.hex(), "after": after.hex()})
    for cell in transaction["permissions"]:
        before, after = bytes.fromhex(cell["before"]), bytes.fromhex(cell["after"])
        for i, (b, a) in enumerate(zip(before, after)):
            if b != a:
                result.append({"kind": "permission.type" if i == 0 else "permission.collision",
                               "map_member": context["map_member"], "tile": [cell["x"], cell["z"]],
                               "rom_offset": base + cell["offset"] + i,
                               "before": bytes([b]).hex(), "after": bytes([a]).hex()})
    return result


# ---- domains: resource-local, so a reused member cannot hide an overlap ------


def legacy_domain(member, proof):
    """Records and cells owned by the qualified M3/M4 planter operation."""
    if proof is None:
        return set()
    return ({("placement", member, 14)}
            | {("permission", member, cell["offset"]) for cell in proof["collision_cells"]})


def transaction_domain(transaction):
    member = transaction["context"]["map_member"]
    return ({("placement", member, p["slot"]) for p in transaction["placements"]}
            | {("permission", member, c["offset"]) for c in transaction["permissions"]})


def describe_domain(item):
    if item[0] == "placement":
        return f"placement {item[1]}:{item[2]}"
    return f"permission byte {item[2]} of map member {item[1]}"


def summarise(transaction, events, context):
    """Preview text shared by the CLI and the native inspector."""
    moved, cells = transaction["placements"], transaction["permissions"]
    unchanged = {"event_member": context["event_member"], "warps": len(events["warps"]),
                 "triggers": len(events["triggers"]), "npcs": len(events["npcs"]),
                 "backgrounds": len(events["backgrounds"]),
                 "note": "record height, BDHC terrain, BGS, doors, warps, triggers and scripts are unchanged"}
    if moved and not cells:
        permission_note = ("movement permissions retained exactly; this coordinate-only edit "
                           "leaves the previous collision where it was")
    elif cells and not moved:
        permission_note = f"{len(cells)} explicitly selected permission cell(s) changed; no placement moved"
    elif cells:
        permission_note = f"{len(cells)} explicitly selected permission cell(s) move with the placement"
    else:
        permission_note = "no change"
    return {"placements_changed": len(moved), "permission_cells_changed": len(cells),
            "delta": transaction["delta"], "permissions": permission_note,
            "unchanged": unchanged, "qualification": NOT_A_QUALIFICATION}


def clone(transaction):
    return copy.deepcopy(transaction)
