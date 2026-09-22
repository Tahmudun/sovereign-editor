"""Baseline-bound planter translations, qualified from native data.

The proofs and their limits are recorded in evidence/m3/README.md and evidence/m4/README.md. No model bounds
or nearby-event query grants write access. This module proposes patches;
core.Project alone persists authored state or writes ROMs.
"""
import json
import math
import struct

from .formats import digest, flat_height_plates, require, resource

QUALIFICATION = "cherrygrove-planter-5-14-west-v1"
BASELINE = "b1ea4b20bbb1f1c22025ac159d390d60f76786ab530851b30ebad239cfa97d4d"
KEY = "5:14"
BEFORE = {"x": 565.5, "z": 404.5}
AFTER = {"x": 564.5, "z": 404.5}
AREA_QUALIFICATION = "cherrygrove-planter-5-14-area-v2"
# An explicit whitelist, NOT a rectangle/bounds-derived editing permission.
TARGETS = ((565.5, 404.5), (564.5, 404.5), (563.5, 404.5),
           (564.5, 405.5), (563.5, 405.5))


def footprint(target):
    x, z = math.floor(target["x"]), math.floor(target["z"])
    return {(x, z + dz) for dz in (-1, 0, 1)}


def require_target(target):
    require(isinstance(target, dict) and set(target) == {"x", "z"}
            and all(type(target[k]) in (int, float) and math.isfinite(target[k]) for k in ("x", "z"))
            and (target["x"], target["z"]) in TARGETS,
            "Unqualified target. Allowed anchors: stock (565.5,404.5), or X 563.5/564.5 at Z 404.5/405.5. "
            "Use whole-tile translations; height stays 1.", "UNQUALIFIED_TARGET")


def _interaction_cells(view):
    """Conservative protected sets; no nearby-model geometry enters this proof."""
    cardinal = ((0, 0), (-1, 0), (1, 0), (0, -1), (0, 1))
    result = []
    for w in view["warps"]:
        result.append(("DOOR_CONFLICT", {(w["x"] + dx, w["z"] + dz) for dx, dz in cardinal}))
    for t in view["triggers"]:
        result.append(("TRIGGER_CONFLICT", {(x, z) for x in range(t["x"], t["x"] + t["width"])
                                           for z in range(t["z"], t["z"] + t["height"])}))
    for b in view["backgrounds"]:
        result.append(("BACKGROUND_CONFLICT", {(b["x"] + dx, b["z"] + dz) for dx, dz in cardinal}))
    for n in view["npcs"]:
        result.append(("OCCUPIED_TILE", {(x + dx, z + dz)
            for x in range(n["x"] - n["range_x"], n["x"] + n["range_x"] + 1)
            for z in range(n["z"] - n["range_z"], n["z"] + n["range_z"] + 1)
            for dx, dz in cardinal}))
    return result


def qualify_area(project, legacy):
    """Recheck EVERY allowed footprint, independent of prior native acceptance.

    The legacy proof establishes source ownership using native planter/house
    controls, and pins unchanged script resources; it is not endpoint playtest
    evidence. This proves static dependencies only, not dynamic script execution.
    """
    base, raw = resource(project.blob, "a/0/6/5", 5)
    start = 20 + struct.unpack_from("<H", raw, 18)[0]
    source = footprint(BEFORE)
    area = set().union(*(footprint({"x": x, "z": z}) for x, z in TARGETS))
    plates = flat_height_plates(raw)
    protected = _interaction_cells(project.base_events)
    cells = []
    for x, z in sorted(area):
        offset = start + 2 * ((z - 384) * 32 + x - 544)
        expected = b"\x00\x80" if (x, z) in source else b"\x00\x00"
        require(raw[offset:offset + 2] == expected,
                f"Unowned collision or non-neutral terrain at {x},{z}", "BEFORE_VALUE_MISMATCH")
        # Entire tile, strictly inside one plate; also refuse any overlapping plate.
        overlaps = [p for p in plates if p["bounds"][0] < x + 1 - 560 and p["bounds"][2] > x - 560
                    and p["bounds"][1] < z + 1 - 400 and p["bounds"][3] > z - 400]
        require(overlaps == [legacy["height_plate"]]
                and overlaps[0]["bounds"][0] < x - 560 < x + 1 - 560 < overlaps[0]["bounds"][2]
                and overlaps[0]["bounds"][1] < z - 400 < z + 1 - 400 < overlaps[0]["bounds"][3],
                f"Unqualified height at {x},{z}", "UNQUALIFIED_HEIGHT")
        for code, occupied in protected:
            require((x, z) not in occupied, f"Planter cell {x},{z} intersects {code.lower()}", code)
        cells.append({"x": x, "z": z, "offset": offset, "rom_offset": base + offset,
                      "before": expected.hex(), "owner": "planter 5:14" if (x, z) in source else "unblocked neutral terrain",
                      "height_plate": 6, "height": 1})

    # Every native-passable perimeter tile of this small local area must remain
    # connected without leaving it. This establishes a local bypass, not a script path.
    domain = {(x, z) for x in range(562, 567) for z in range(402, 408)}
    def passable(cell):
        x, z = cell
        offset = start + 2 * ((z - 384) * 32 + x - 544)
        kind, flags = raw[offset:offset + 2]
        return not flags & 128 and kind not in (16, 21)
    perimeter = {c for c in domain if (c[0] in (562, 566) or c[1] in (402, 407)) and passable(c)}
    for x, z in TARGETS:
        blocked = footprint({"x": x, "z": z})
        walkable = ({c for c in domain if passable(c)} | source) - blocked
        pending = [min(perimeter)]
        reached = set(pending)
        while pending:
            xx, zz = pending.pop()
            for c in ((xx - 1, zz), (xx + 1, zz), (xx, zz - 1), (xx, zz + 1)):
                if c in walkable and c not in reached:
                    reached.add(c)
                    pending.append(c)
        require(perimeter <= reached, f"Planter at {x},{z} interrupts local walking connections", "PATH_CONFLICT")

    dependencies = {"baseline_sha256": BASELINE, "source_ownership_sha256": legacy["dependencies_sha256"],
                    "resources": legacy["dependencies"]["resources"], "events": project.base_events,
                    "allowed_anchors": [{"x": x, "z": z} for x, z in TARGETS], "area_cells": cells,
                    "height_plate": legacy["height_plate"],
                    "interaction_policy": "doors/backgrounds and cardinal approaches; complete triggers; NPC ranges and cardinal approaches",
                    "local_bypass": {"bounds": [562, 402, 567, 408], "perimeter": sorted(perimeter), "all_targets_connected": True},
                    "script_scope": "resources pinned unchanged; dynamic paths and guide scene untested"}
    return {"id": AREA_QUALIFICATION, "key": KEY, "before": dict(BEFORE),
            "record_before": legacy["record_before"], "dependencies": dependencies,
            "dependencies_sha256": digest(json.dumps(dependencies, sort_keys=True, separators=(",", ":")).encode()),
            "allowed_anchors": dependencies["allowed_anchors"], "area_cells": cells,
            "height_plate": legacy["height_plate"], "record_rom_offset": base + project.maps[5][1][14]["record_offset"],
            "collision_updates_required": True, "direct_event_conflicts": []}


def translation_proof(area, target):
    require_target(target)
    source, destination = footprint(BEFORE), footprint(target)
    cells, patches = [], []
    for cell in area["area_cells"]:
        xy = (cell["x"], cell["z"])
        if xy not in source | destination:
            continue
        before = bytes.fromhex(cell["before"])
        after = bytes((before[0], (before[1] & ~128) | (128 if xy in destination else 0)))
        cells.append({**cell, "after": after.hex()})
        if before != after:
            patches.append({"kind": "collision.flag", "rom_offset": cell["rom_offset"] + 1,
                            "before": before[1:].hex(), "after": after[1:].hex()})
    for axis, offset, origin in (("x", 4, 560), ("z", 12, 400)):
        if target[axis] != BEFORE[axis]:
            patches.append({"kind": f"placement.{axis}", "rom_offset": area["record_rom_offset"] + offset,
                            "before": bytes.fromhex(area["record_before"])[offset:offset + 4].hex(),
                            "after": struct.pack("<i", int((target[axis] - origin) * 65536)).hex()})
    return {**area, "after": dict(target), "collision_cells": cells, "patches": patches}


def qualify_move(project):
    require(project.doc["baseline"]["sha256"] == BASELINE,
            "Decoration movement requires the M3 qualified executable and resources", "UNQUALIFIED_DEPENDENCIES")
    base, raw = resource(project.blob, "a/0/6/5", 5)
    prop = project.maps[5][1][14]
    require(prop["model_id"] == 52 and prop["xyz"] == [5.5, 1, 4.5]
            and prop["rotation_raw"] == [0, 0, 0] and prop["scale_raw"] == [4096] * 3,
            "Planter source transform differs", "BEFORE_VALUE_MISMATCH")
    record = raw[prop["record_offset"]:prop["record_offset"] + 48]
    start = 20 + struct.unpack_from("<H", raw, 18)[0]

    def permission(x, z):
        offset = start + 2 * ((z - 384) * 32 + x - 544)
        return offset, raw[offset:offset + 2]

    # Native controls: identical house 37 has a passable western flank in slot 6.
    # Its blocked body and door pattern matches slot 10. All three planter anchors
    # carry a 1x3 permission stamp. These are data comparisons, not mesh bounds.
    houses = project.maps[5][1]
    require(houses[6]["model_id"] == houses[10]["model_id"] == 37
            and houses[6]["xyz"] == [-12, 1, -1.5]
            and houses[10]["xyz"] == [8, 1, 4.5], "House control differs")
    for dz in (-1, 0, 1):
        require(permission(545, 398 + dz)[1] == b"\x00\x04", "House control flank differs")
        for dx in (-2, -1, 0, 1):
            require(permission(548 + dx, 398 + dz)[1] == permission(568 + dx, 404 + dz)[1],
                    "House body/door control differs")
        for x, z in ((550, 398), (561, 400), (565, 404)):
            require(permission(x, z + dz)[1] == b"\x00\x80", "Planter collision control differs")

    cells, patches = [], []
    for x, before, after in ((565, b"\x00\x80", b"\x00\x00"), (564, b"\x00\x00", b"\x00\x80")):
        for z in (403, 404, 405):
            offset, actual = permission(x, z)
            require(actual == before, "Collision before-value differs", "BEFORE_VALUE_MISMATCH")
            cells.append({"x": x, "z": z, "offset": offset, "before": before.hex(), "after": after.hex()})
            # Patch only the blocking flag byte. Behavior and all other bits survive.
            patches.append({"kind": "collision.flag", "rom_offset": base + offset + 1,
                            "before": before[1:].hex(), "after": after[1:].hex()})

    # Both entire tile strips fit strictly inside the same native horizontal plate.
    plates = flat_height_plates(raw)
    shared = [p for p in plates if p["bounds"][0] < 4 and p["bounds"][1] < 3
              and p["bounds"][2] > 6 and p["bounds"][3] > 6 and p["height"] == 1]
    require(len(shared) == 1 and shared[0]["index"] == 6, "Source/destination height dependency differs")
    view = project.base_events
    for cell in cells:
        x, z = cell["x"], cell["z"]
        require(not any(w["x"] == x and w["z"] <= z <= w["z"] + 1 for w in view["warps"]),
                "Planter conflicts with a door or its approach", "DOOR_CONFLICT")
        require(not any(t["x"] <= x < t["x"] + t["width"] and t["z"] <= z < t["z"] + t["height"]
                        for t in view["triggers"]), "Planter intersects a scene trigger", "TRIGGER_CONFLICT")
        # Background interactions also include one-tile cardinal approaches.
        require(not any(abs(b["x"] - x) + abs(b["z"] - z) <= 1 for b in view["backgrounds"]),
                "Planter conflicts with a background interaction", "BACKGROUND_CONFLICT")
        require(not any(abs(n["x"] - x) <= n["range_x"] and abs(n["z"] - z) <= n["range_z"]
                        for n in view["npcs"]), "Planter intersects a stock actor range", "OCCUPIED_TILE")

    refs = []
    for archive, member in (("a/0/6/5", 5), ("a/0/3/2", 64), ("a/0/1/2", 850), ("a/0/1/2", 623),
                            ("a/0/4/0", 37), ("a/0/4/0", 52)):
        _, data = resource(project.blob, archive, member)
        refs.append({"archive": archive, "member": member, "sha256": digest(data)})
    dependencies = {"baseline_sha256": BASELINE, "resources": refs, "collision_cells": cells,
                    "height_plate": shared[0], "events": view}
    dependency_hash = digest(json.dumps(dependencies, sort_keys=True, separators=(",", ":")).encode())
    # X is the only translated component; retain Y/Z, rotation, scale and unknown tail.
    offset = base + prop["record_offset"] + 4
    patches.insert(0, {"kind": "placement.x", "rom_offset": offset,
                       "before": struct.pack("<i", 360448).hex(), "after": struct.pack("<i", 294912).hex()})
    return {"id": QUALIFICATION, "key": KEY, "before": dict(BEFORE), "after": dict(AFTER),
            "record_before": record.hex(), "dependencies_sha256": dependency_hash,
            "collision_updates_required": True, "collision_cells": cells, "height_plate": shared[0],
            "direct_event_conflicts": [], "dependencies": dependencies, "patches": patches}


def authored_move(proof):
    return {k: proof[k] for k in ("id", "before", "after", "record_before", "dependencies_sha256")}
