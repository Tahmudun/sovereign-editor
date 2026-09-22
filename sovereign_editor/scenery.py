"""Explicit placed-object lifecycle, using stable authoring slots and raw records.

Baseline slots never shift in authored history. Created slots are baseline count
plus transaction index; export alone assigns dense slots. Each object also keeps
an identity across transfers. DSPRE's 48-byte layout is adapted; unknown words
are copied, never synthesized. See references/dspre/PROVENANCE.json.
"""
import copy
import math
import struct

from . import authoring, world
from .formats import (digest, require, resource, map_sections, map_data,
                      flat_height_plates, EditorError)

SCHEMA = "sovereign-scenery-transaction-v1"


def is_transaction(value):
    return isinstance(value, dict) and value.get("schema") == SCHEMA


def words(record):
    return dict(zip(("x", "y", "z"), struct.unpack_from("<3i", record, 4)))


def table_for(project, context, state):
    member = context["map_member"]
    if member not in state["objects"]:
        raw = project.member_raw(member)
        table = {}
        for prop in project.member_data(member)[1]:
            slot = prop["slot"]
            data = bytearray(raw[prop["record_offset"]:prop["record_offset"] + 48])
            position = state["placements"].get((member, slot))
            if position is not None:
                struct.pack_into("<3i", data, 4, *(position[a] for a in ("x", "y", "z")))
            table[slot] = {"id": f"baseline:{member}:{slot}", "raw": bytes(data)}
        state["objects"][member] = table
    return state["objects"][member]


def properties(project, context, state):
    table = table_for(project, context, state)
    raw = project.member_raw(context["map_member"])
    sections = map_sections(raw)
    # Reuse the validated format reader for fields, assigning stable slots after parsing.
    records = b"".join(o["raw"] for o in table.values())
    rebuilt = bytearray(raw[:sections["buildings_offset"]] + records + raw[sections["model_offset"]:])
    struct.pack_into("<I", rebuilt, 4, len(records))
    props = map_data(rebuilt)[1]
    for export_slot, (prop, (slot, obj)) in enumerate(zip(props, table.items())):
        prop.update(slot=slot, object_id=obj["id"], export_slot=export_slot,
                    authored_new=not obj["id"].startswith(f"baseline:{context['map_member']}:"))
    return props


def asset_dependency(project, context, record):
    from . import mapscene
    model_id = struct.unpack_from("<I", record)[0]
    refs = context["resources"]
    archive = refs["building_models"]["archive"]
    tex = refs["building_textures"]
    key = (project.doc["baseline"]["sha256"], archive, model_id, tex["member"])
    if not hasattr(project, "_scenery_assets"):
        project._scenery_assets = {}
    if key not in project._scenery_assets:
        _, texture = resource(project.blob, tex["archive"], tex["member"])
        source, summary, _ = mapscene.building_model(project, archive, model_id, texture)
        bounds = summary["bounds"]
        require((bounds[1][0] - bounds[0][0]) * (bounds[1][2] - bounds[0][2]) / 256
                <= mapscene.AMBIENT_TILES,
                "Ambient scene models are not scenery templates", "UNSUPPORTED_TEMPLATE")
        project._scenery_assets[key] = {"model": {"archive": archive, "member": model_id,
                                                   "sha256": source["sha256"]},
                                         "texture": {**tex, "sha256": digest(texture)},
                                         "name": summary["name"]}
    mapscene._placement_transform({"key": str(model_id), "rotation_raw": list(struct.unpack_from("<3i", record, 16)),
                                   "scale_raw": list(struct.unpack_from("<3i", record, 28))})
    return copy.deepcopy(project._scenery_assets[key])


def floor_height(project, context, position):
    x = position["x"] - context["origin"][0] - 16
    z = position["z"] - context["origin"][1] - 16
    plates = flat_height_plates(project.member_raw(context["map_member"]))
    heights = {p["height"] for p in plates if p["bounds"][0] <= x < p["bounds"][2]
               and p["bounds"][1] <= z < p["bounds"][3]}
    require(len(heights) == 1, "Transfer needs one verified flat height at each anchor",
            "UNSUPPORTED_HEIGHT")
    return next(iter(heights))


def protected(obj, member, slot, state):
    if obj["id"] == "baseline:0:13":
        return "This town sign has a bound text interaction; use its existing move/alignment controls."
    if ("placement", member, slot) in state["legacy_domain"]:
        return "This object belongs to the qualified planter operation; undo that operation first."
    return None


def plan(project, context, state, index, operation, slot, x=None, z=None,
         destination=None, permissions=(), destination_permissions=(), move_collision=(), label=None):
    require(operation in ("add", "duplicate", "delete", "transfer", "move", "import"), "Unknown scenery operation")
    require(type(slot) is int and slot >= 0, "Choose a stable authoring slot")
    require(label is None or isinstance(label, str) and len(label) <= 160, "Use a short text label")
    require(isinstance(permissions, (list, tuple)) and isinstance(destination_permissions, (list, tuple))
            and isinstance(move_collision, (list, tuple)), "Permission selections must be lists")
    member = context["map_member"]
    table = table_for(project, context, state)
    if operation in ("add", "import"):
        props = project.member_data(member)[1]
        require(slot < len(props), "Palette template is absent", "NOT_FOUND")
        offset = props[slot]["record_offset"]
        obj = {"id": f"baseline:{member}:{slot}", "raw": project.member_raw(member)[offset:offset + 48]}
    else:
        require(slot in table, "Object was removed or transferred; refresh the map", "NOT_FOUND")
        obj = table[slot]
    reason = protected(obj, member, slot, state)
    require(reason is None or operation == "duplicate" and obj["id"] != "baseline:0:13",
            reason or "Protected object", "BOUND_OBJECT")
    if destination is None:
        target = context
    else:
        require(isinstance(destination, dict) and set(destination) == {"header", "cell"},
                "Destination needs header and cell")
        target = project.context(**destination)
    target_member = target["map_member"]
    if operation == "transfer":
        require(target_member != member, "Cannot transfer between occurrences of the same map resource",
                "SHARED_RESOURCE")
        require(target["matrix"]["id"] == context["matrix"]["id"] and
                max(abs(context["cell"][a] - target["cell"][a]) for a in ("x", "y")) == 1,
                "Choose an adjacent cell in the same matrix", "NOT_ADJACENT")
    elif operation == "import":
        require(destination is not None, "Import needs a destination context")
    else:
        require(destination is None, "Only transfer accepts a destination cell")
        require(not destination_permissions, "Destination permissions require a transfer")
    before = authoring.global_from_record(context, words(obj["raw"]))
    if operation == "delete":
        require(x is None and z is None and not move_collision, "Delete accepts explicit collision cells only")
        after = None
    else:
        require(x is not None and z is not None, "Choose target X and Z")
        after = {"x": x, "y": before["y"], "z": z}
        authoring.require_anchor(target, after)
    # Deletion is possible even if the preview format is unsupported. Other
    # operations need a renderable template and verified asset dependencies.
    assets = None if operation == "delete" else asset_dependency(project, context, obj["raw"])
    if operation in ("transfer", "import"):
        require(context["resources"]["building_models"] == target["resources"]["building_models"]
                and context["resources"]["building_textures"] == target["resources"]["building_textures"],
                "Destination uses a different model archive or texture set", "INCOMPATIBLE_ASSETS")
        require(context["cell"]["altitude"] == target["cell"]["altitude"]
                and floor_height(project, context, before) == floor_height(project, target, after),
                "Transfer would change the object's ground height", "UNSUPPORTED_HEIGHT")
        require(asset_dependency(project, target, obj["raw"]) == assets,
                "Destination asset binding differs", "INCOMPATIBLE_ASSETS")
    changes = []
    if operation in ("delete", "transfer"):
        changes.append({"member": member, "slot": slot, "id": obj["id"],
                        "before": obj["raw"].hex(), "after": None})
    if operation != "delete":
        record = bytearray(obj["raw"])
        position = authoring.record_from_global(target, after, words(record))
        struct.pack_into("<3i", record, 4, *(position[a] for a in ("x", "y", "z")))
        new_slot = slot if operation == "move" else len(project.member_data(target_member)[1]) + index
        identity = obj["id"] if operation in ("move", "transfer") else f"created:{index}"
        if operation != "move":
            require(len(table_for(project, target, state)) < 32,
                    'Destination already has 32 placed objects, the verified map-prop capacity', 'RESOURCE_CAPACITY')
            require(new_slot not in table_for(project, target, state), "Created object ID already exists", "STALE_EDIT")
        if operation != "move" or bytes(record) != obj["raw"]:
            changes.append({"member": target_member, "slot": new_slot, "id": identity,
                            "before": obj["raw"].hex() if operation == "move" else None,
                            "after": record.hex()})
    selections = {}
    cell_contexts = {member: context, target_member: target}
    def select(ctx, c):
        authoring.require_cell_request(c)
        key = (ctx["map_member"], c["x"], c["z"])
        if key in selections:
            require(selections[key]["after"].lower() == c["after"].lower(), "Conflicting collision selections", "CELL_CONFLICT")
            a, b = selections[key].get("before"), c.get("before")
            require(a is None or b is None or a.lower() == b.lower(), "Conflicting before-values", "CELL_CONFLICT")
            if b is not None:
                selections[key]["before"] = b
        else:
            selections[key] = dict(c)
    for c in permissions:
        select(context, c)
    for c in destination_permissions:
        select(target, c)
    if move_collision:
        dx, dz = after["x"] - before["x"], after["z"] - before["z"]
        require(all(float(v).is_integer() for v in (dx, dz)), "Collision selection requires whole-tile movement")
        source_cells, destination_cells = set(), set()
        for c in move_collision:
            require(isinstance(c, dict) and set(c) == {"x", "z"}
                    and all(type(v) is int for v in c.values()), "Selected collision cells need integer x/z")
            offset = world.cell_offset(context, c["x"], c["z"])
            pair = state["permissions"].get((member, offset), project.member_raw(member)[offset:offset + 2])
            require(pair[1] & 128, "Selected source cell is not blocked", "BEFORE_VALUE_MISMATCH")
            source_cells.add((member, c["x"], c["z"]))
            destination_cells.add((target_member, c["x"] + int(dx), c["z"] + int(dz)))
        affected = destination_cells | (source_cells if operation in ("move", "transfer") else set())
        for m, xx, zz in sorted(affected):
            ctx = cell_contexts[m]
            offset = world.cell_offset(ctx, xx, zz)
            pair = state["permissions"].get((m, offset), project.member_raw(m)[offset:offset + 2])
            flag = 128 if (m, xx, zz) in destination_cells else 0
            value = bytes((pair[0], (pair[1] & ~128) | flag))
            select(ctx, {"x": xx, "z": zz, "before": pair.hex(), "after": value.hex()})
    cells = []
    for (m, xx, zz), c in sorted(selections.items()):
        ctx = cell_contexts[m]; offset = world.cell_offset(ctx, xx, zz)
        pair = state["permissions"].get((m, offset), project.member_raw(m)[offset:offset + 2])
        require(c.get("before") is None or c["before"].lower() == pair.hex(), "Permission before-value differs", "BEFORE_VALUE_MISMATCH")
        value = bytes.fromhex(c["after"])
        require(value[0] == pair[0] and (value[1] & ~128) == (pair[1] & ~128),
                "Scenery collision edits preserve type and non-blocking bits", "UNSUPPORTED_EDIT")
        require(("permission", m, offset) not in state["legacy_domain"], "Permission belongs to qualified planter", "LEGACY_CONFLICT")
        if value != pair:
            cells.append({"member": m, "x": xx, "z": zz, "offset": offset,
                          "before": pair.hex(), "after": value.hex()})
    request = {"operation": operation, "slot": slot, "x": x, "z": z,
               "destination": {"header": target["header"]["id"], "cell": [target["cell"]["x"], target["cell"]["y"]]} if destination else None,
               "permissions": list(permissions), "destination_permissions": list(destination_permissions),
               "move_collision": list(move_collision), "label": label}
    deps = {"contexts": [authoring.dependencies(c, index) for c in cell_contexts.values()],
            "assets": assets, "donor_id": obj["id"], "donor_sha256": digest(obj["raw"])}
    return {"schema": SCHEMA, "version": 1, "index": index,
            "label": label or f"{operation.capitalize()} scenery", "context": authoring.context_ref(context),
            "destination_context": authoring.context_ref(target), "request": request,
            "objects": changes, "permissions": cells, "dependencies": deps,
            "dependencies_sha256": authoring.canonical(deps), "qualification": authoring.NOT_A_QUALIFICATION,
            "from": before, "to": after}


def replay(project, state, transaction, index):
    require(isinstance(transaction.get("request"), dict), "Missing scenery request", "UNSUPPORTED_EDIT")
    ref = transaction.get("context", {})
    context = project.context(header=ref.get("header"), cell=ref.get("cell"))
    require(authoring.context_ref(context) == ref, "Scenery context differs", "CONTEXT_MISMATCH")
    try:
        expected = plan(project, context, state, index, **transaction["request"])
    except TypeError as exc:
        raise EditorError("UNSUPPORTED_EDIT", "Invalid scenery request fields") from exc
    require(transaction == expected, "Scenery identity, before-values or dependencies differ", "BEFORE_VALUE_MISMATCH")
    for change in transaction["objects"]:
        m, slot = change["member"], change["slot"]
        table = state["objects"][m]
        if change["after"] is None:
            del table[slot]
        else:
            data = bytes.fromhex(change["after"])
            table[slot] = {"id": change["id"], "raw": data}
            state["placements"][(m, slot)] = words(data)
        state["structural_members"].add(m)
    for cell in transaction["permissions"]:
        key = cell["member"], cell["offset"]
        state["permissions"][key] = bytes.fromhex(cell["after"])
        state["generic"]["permissions"][key] = bytes.fromhex(cell["after"])
    target = project.context(header=transaction["destination_context"]["header"],
                             cell=transaction["destination_context"]["cell"])
    state["contexts"].extend([context, target] if context["id"] != target["id"] else [context])


def summary(transaction):
    return {"operation": "scenery.transaction", "index": transaction["index"],
            "label": transaction["label"], "context": transaction["context"],
            "destination_context": transaction["destination_context"],
            "action": transaction["request"]["operation"], "from": transaction["from"], "to": transaction["to"],
            "objects": [{"member": o["member"], "slot": o["slot"], "id": o["id"],
                         "action": "remove" if o["after"] is None else "add" if o["before"] is None else "move"}
                        for o in transaction["objects"]], "permission_cells": transaction["permissions"]}
