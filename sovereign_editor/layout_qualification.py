"""Read-only, fail-closed M5 prerequisite proofs; never a placement write grant.

No UI/CLI operations import this module. core.Project remains the only writer.
Regenerate definitions from Project before using them; JSON status fields are
reports, not capabilities. M3/M4 records and their weaker historical scope remain
unchanged. Native audit and unresolved semantics: evidence/m5-qualification/.
"""
import copy
import math
import struct
from collections import deque

import ndspy.codeCompression

from .formats import digest, events, map_data, require, resource, span
from .native_layout import (actor_reference, initialization, movement_definitions,
                            movement_stream, native_evidence, project_event_routes, runtime_evidence,
                            item_runtime_inputs)
from .runtime_contracts import COMMAND_CONTRACTS, child_serialization, contract_sites

BASELINE = "b1ea4b20bbb1f1c22025ac159d390d60f76786ab530851b30ebad239cfa97d4d"
SCHEMA = "cherrygrove-layout-prerequisite-v3"
MAP = "a/0/6/5"
SCRIPTS = "a/0/1/2"
ORIGIN = (560, 400)
ANCHORS = tuple((x + .5, z + .5) for x in (551, 552) for z in range(397, 403)) + tuple(
    (x + .5, 404.5) for x in range(551, 562))
ORIGINAL_PROPOSAL = {"5:12": [551.5, 399.5], "5:13": [556.5, 404.5], "5:14": [560.5, 404.5]}
PROPOSAL = {"5:12": [551.5, 399.5], "5:13": [559.5, 404.5], "5:14": [560.5, 404.5]}


def canonical_hash(value):
    import json
    return digest(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())


def refusal(code, **detail):
    return {"code": code, **detail}


def footprint(anchor):
    require(isinstance(anchor, (list, tuple)) and len(anchor) == 2
            and all(type(v) in (int, float) and math.isfinite(v) and v % 1 == .5 for v in anchor),
            "Layout anchors must be finite half-tile X/Z values", "UNQUALIFIED_TARGET")
    x, z = map(math.floor, anchor)
    return {(x, z - 1), (x, z), (x, z + 1)}


def terrain(raw):
    """Exact fixed-point BDHC rectangles AND strip-index dependencies."""
    permissions, placements, model, size = struct.unpack_from("<4I", raw)
    offset = 20 + struct.unpack_from("<H", raw, 18)[0] + permissions + placements + model
    data = span(raw, offset, size)
    require(data[:4] == b"BDHC", "Missing BDHC")
    counts = struct.unpack_from("<6H", span(data, 4, 12))
    cursor, arrays, records = 16, [], []
    for name, count, fmt in zip(("points", "normals", "constants", "plates", "strips", "indices"),
                                counts, ("<2i", "<3i", "<i", "<4H", "<i2H", "<H")):
        width = struct.calcsize(fmt)
        section = span(data, cursor, count * width)
        arrays.append(list(struct.iter_unpack(fmt, section)))
        records.append({"section": name, "map_offset": offset + cursor, "count": count,
                        "record_size": width, "before": section.hex(), "sha256": digest(section)})
        cursor += count * width
    require(cursor == len(data), "BDHC trailing bytes")
    points, normals, constants, plates, strips, indices = arrays
    result = []
    for index, (first, second, normal, constant) in enumerate(plates):
        require(max(first, second) < len(points) and normal < len(normals) and constant < len(constants),
                "BDHC index out of bounds")
        require(normals[normal] == (0, 4096, 0), "Sloped height plane unsupported", "UNQUALIFIED_HEIGHT")
        x0, z0 = points[first]; x1, z1 = points[second]
        require(x0 < x1 and z0 < z1, "Invalid height rectangle")
        result.append({"index": index, "bounds_raw": [x0, z0, x1, z1],
                       "height_raw": -constants[constant][0]})
    bands = []
    for upper, count, start in strips:
        require(start + count <= len(indices), "BDHC strip outside index array")
        ids = [v[0] for v in indices[start:start + count]]
        require(all(i < len(plates) for i in ids), "BDHC strip plate outside array")
        bands.append({"upper_raw": upper, "plates": ids})
    require(all(a["upper_raw"] < b["upper_raw"] for a, b in zip(bands, bands[1:])),
            "BDHC strip thresholds are not increasing")
    return {"offset": offset, "size": size, "sha256": digest(data), "records": records,
            "plates": result, "strips": bands}


def height_coverage(data, cell, height=65536):
    """Closed whole-tile cover; disallow gaps, interior overlap and edge ties
    at different heights. Exact partition, including half-integer midpoints.

    At strip thresholds BOTH incident strip lists must provide a covering plane.
    This avoids choosing an unproven tie direction. It is a geometric/index proof,
    not reverse engineering of every native height lookup caller.
    """
    x, z = cell
    box = [(x - ORIGIN[0]) * 65536, (z - ORIGIN[1]) * 65536,
           (x + 1 - ORIGIN[0]) * 65536, (z + 1 - ORIGIN[1]) * 65536]
    ps = [p for p in data["plates"] if p["bounds_raw"][0] <= box[2] and p["bounds_raw"][2] >= box[0]
          and p["bounds_raw"][1] <= box[3] and p["bounds_raw"][3] >= box[1]]
    xs = sorted({box[0], box[2]} | {v for p in ps for v in p["bounds_raw"][::2] if box[0] < v < box[2]})
    zs = sorted({box[1], box[3]} | {v for p in ps for v in p["bounds_raw"][1::2] if box[1] < v < box[3]}
                | {s["upper_raw"] for s in data["strips"] if box[1] < s["upper_raw"] < box[3]})
    failures, used, incident = [], set(), set()
    # Use doubled coordinates so midpoint arithmetic is integral, never epsilon-based.
    def covering(xx, zz, strict=False):
        if strict:
            return [p for p in ps if 2*p["bounds_raw"][0] < xx < 2*p["bounds_raw"][2]
                    and 2*p["bounds_raw"][1] < zz < 2*p["bounds_raw"][3]]
        return [p for p in ps if 2*p["bounds_raw"][0] <= xx <= 2*p["bounds_raw"][2]
                and 2*p["bounds_raw"][1] <= zz <= 2*p["bounds_raw"][3]]
    for left, right in zip(xs, xs[1:]):
        for top, bottom in zip(zs, zs[1:]):
            hit = covering(left + right, top + bottom, True)
            used.update(p["index"] for p in hit)
            if len(hit) != 1 or hit[0]["height_raw"] != height:
                failures.append("HEIGHT_GAP_OVERLAP_OR_LEVEL")
    samples_x = sorted({2*v for v in xs} | {a+b for a,b in zip(xs,xs[1:])})
    samples_z = sorted({2*v for v in zs} | {a+b for a,b in zip(zs,zs[1:])})
    for xx in samples_x:
        for zz in samples_z:
            hit = covering(xx, zz)
            incident.update(p["index"] for p in hit)
            if not hit or any(p["height_raw"] != height for p in hit):
                failures.append("HEIGHT_BOUNDARY_AMBIGUITY")
            ids = {p["index"] for p in hit if p["height_raw"] == height}
            bands = [s for i,s in enumerate(data["strips"])
                     if (i == 0 or 2*data["strips"][i-1]["upper_raw"] <= zz) and zz <= 2*s["upper_raw"]]
            if not bands or any(not ids.intersection(s["plates"]) for s in bands):
                failures.append("HEIGHT_STRIP_COVERAGE")
    return {"cell": list(cell), "qualified": not failures, "interior_plates": sorted(used),
            "incident_plates": sorted(incident), "height_raw": height,
            "refusals": sorted(set(failures))}


def interaction_sets(view):
    """Complete records, ranges, and cardinal approaches, labeled by identity."""
    result = []
    for kind in ("warps", "triggers", "backgrounds", "npcs"):
        for record in view[kind]:
            x, z = record["x"], record["z"]
            if kind == "triggers":
                cells = {(xx, zz) for xx in range(x, x+record["width"]) for zz in range(z,z+record["height"])}
            else:
                rx, rz = (record["range_x"], record["range_z"]) if kind == "npcs" else (0,0)
                cells = {(xx+dx,zz+dz) for xx in range(x-rx,x+rx+1) for zz in range(z-rz,z+rz+1)
                         for dx,dz in ((0,0),(-1,0),(1,0),(0,-1),(0,1))}
            result.append({"kind": kind, "id": record["id"], "cells": sorted(cells)})
    return result


def permission_cell(raw, cell, member=5):
    x,z = cell
    require(544 <= x < 576 and 384 <= z < 416, "Outside bounded map", "OUTSIDE_SCOPE")
    offset = 20 + struct.unpack_from("<H", raw, 18)[0] + 2*((z-384)*32+x-544)
    return {"cell": [x,z], "member": member, "offset": offset, "before": span(raw, offset, 2).hex()}


def ownership(maps):
    """Native controls grant clearance only on matched front-body/door rows.
    Model 38's fractional mismatch is an explicit unresolved reservation.
    """
    def relative(member, slot, dx, dz):
        raw = maps[member]
        _, props, _ = map_data(raw); q = props[slot]
        x, z = math.floor(q["xyz"][0]+16)+dx, math.floor(q["xyz"][2]+16)+dz
        require(0 <= x < 32 and 0 <= z < 32, "Control outside map")
        offset = 20+struct.unpack_from("<H",raw,18)[0]+2*(z*32+x)
        return {"member": member, "slot": slot, "delta": [dx,dz], "offset": offset,
                "before": span(raw,offset,2).hex()}
    def matched(source_slot, control_member, control_slot):
        a = map_data(maps[5])[1][source_slot]; b = map_data(maps[control_member])[1][control_slot]
        require(a["model_id"] == b["model_id"] == 37 and
                a["rotation_raw"] == b["rotation_raw"] == [0,0,0] and
                a["scale_raw"] == b["scale_raw"] == [4096]*3 and
                all(a["xyz"][i] % 1 == b["xyz"][i] % 1 for i in (0,2)), "House control transforms differ")
        pairs = [(relative(5,source_slot,dx,dz),relative(control_member,control_slot,dx,dz))
                 for dz in (-1,0,1) for dx in (-2,-1,0,1)]
        require(all(a["before"] == b["before"] for a,b in pairs), "House front body/door control differs")
        return {"body_door_pairs": pairs, "source_house_xyz": a["xyz"], "control_house_xyz": b["xyz"],
                "policy": "matched X/Z fractions, neutral transform and all 12 front body/door cells; Y separately qualified"}
    proofs = {12: matched(6,8,1), 14: matched(10,5,6)}
    require(not any(p["model_id"] == 52 for p in map_data(maps[8])[1]), "Member-8 planter-free control changed")
    sources = {}
    for slot, house in ((12,6),(13,8),(14,10)):
        prop = map_data(maps[5])[1][slot]
        require(prop["model_id"] == 52 and prop["rotation_raw"] == [0,0,0] and prop["scale_raw"] == [4096]*3,
                "Source transform changed")
        anchor = [prop["xyz"][0]+560,prop["xyz"][2]+400]
        cells = [permission_cell(maps[5],c) for c in sorted(footprint(anchor))]
        require(all(c["before"] == "0080" for c in cells), "Source stamp changed", "BEFORE_VALUE_MISMATCH")
        control = proofs.get(slot)
        if control:
            flank = [relative(8,1,2,dz) if slot == 12 else relative(5,6,-3,dz) for dz in (-1,0,1)]
            require(all(c["before"] == "0004" for c in flank), "Passable control flank changed")
            control["passable_flank"] = flank
        for c in cells:
            c.update(exclusive_owner=f"5:{slot}" if control else None,
                     retained_owners=[] if control else ["unresolved:house-38-or-environment"],
                     clear_allowed=bool(control))
        sources[f"5:{slot}"] = {"anchor": anchor, "height_raw": int(prop["xyz"][1]*65536),
            "record_offset": prop["record_offset"], "record_before": span(maps[5],prop["record_offset"],48).hex(),
            "record_sha256": prop["record_sha256"], "cells": cells, "control": control,
            "ownership_qualified": bool(control), "refusals": [] if control else [refusal(
                "OWNERSHIP_AMBIGUOUS", member=5, slot=13, house_slot=8,
                control_member=296, control_slot=0, source_house_xyz=[-1,1,.5],
                control_house_xyz=[-9.5,1,-13.5],
                retained_cells=[c["cell"] for c in cells],
                resolution="Matching X/Z-aligned house-38 control without planter or native per-object collision ownership evidence; never clear these blockers from stamp repetition.")]}
    sources["5:13"]["ambiguous_control"] = [relative(296,0,2,dz) for dz in (-1,0,1)]
    pairs = [(relative(5,8,dx,dz),relative(296,0,dx,dz))
             for dz in (-1,0,1) for dx in (-2,-1,0,1)]
    sources['5:13']['ownership_investigation'] = {
        'body_door_pairs':pairs,
        'body_door_mismatches':[(a,b) for a,b in pairs if a['before'] != b['before']],
        'source_east_cell_x_relative_to_house':[2,3],
        'control_east_cell_x_relative_to_house':[1.5,2.5],
        'control_wider_blocked_context':[relative(296,0,dx,dz) for dz in (-1,0) for dx in (3,4)],
        'runtime_collision_query':{'handler_address':0x020548c0,'reader_address':0x020547d8,
            'value':'bit 15 of the aggregate permission halfword indexed by map and tile; no owner argument'},
        'compatible_ownership_hypotheses':['planter exclusive, underlying house/environment bit clear',
                                          'shared with house/environment, underlying bit set'],
        'conclusion':'Native aggregate collision bytes cannot distinguish these hypotheses. The control also '
                     'fails the door-behavior match, independently of its half-tile X mismatch.',
        'missing_evidence':'Independent house-38 and planter collision authoring masks at the exact source '
                           'alignment, or a planter-free matching body/door native control. Model bounds and '
                           'the repeated blocked flank cannot supply the missing provenance.'}
    return sources


def entry_table(raw):
    offsets, cursor = [], 0
    while span(raw,cursor,2) != b"\x13\xfd":
        require(cursor < 4096, "Script entry table exceeds bounded limit")
        offset = cursor+4+struct.unpack_from("<I",span(raw,cursor,4))[0]
        require(offset+2 <= len(raw), "Script entry outside member")
        offsets.append(offset); cursor += 4
    require(all(v >= cursor+2 for v in offsets), "Script entry overlaps table")
    return offsets, cursor+2


# Only cursor consumption / structural control is audited. Effects are NOT
# certified by these shapes. Every unresolved effect remains a blocking frontier.
SHAPES = {2:(0,"end"),0x14:(2,"dispatch"),0x1c:(5,"branch"),0x20:(2,"effect"),
          0x49:(2,"effect"),0x5e:(6,"movement"),0x5f:(0,"wait_movement"),0x62:(2,"effect"),
          0x15:(0,"child_release"),0x16:(4,"jump"),0x1a:(4,"call"),0x1b:(0,"return"),
          0x1f:(2,"effect"),0x38:(3,"effect"),0x4b:(2,"effect"),0x51:(2,"effect"),
          0x60:(0,"effect"),0x69:(4,"effect"),0x126:(4,"effect"),0x261:(0,"effect"),
          0x37:(6,"effect"),0x39:(1,"effect"),0x2a:(4,"effect"),0x54:(4,"effect"),
          0x68:(0,"effect"),0x11:(4,"effect"),0x57:(2,"effect"),0x64:(2,"effect"),
          0x3a:(0,"effect"),0x2d:(1,"effect"),0x1b6:(4,"effect"),0x7f:(6,"effect"),
          0x1e4:(2,"effect"),0x1e:(2,"effect"),0x153:(10,"effect"),
          0x3b:(3,"effect"),0x3c:(2,"effect"),0x32:(0,"effect"),0x1b8:(4,"effect"),
          0x29:(4,"effect"),0x7d:(6,"effect"),0xc2:(3,"effect"),0x82:(4,"effect"),
          0x35:(0,"effect"),0xbe:(1,"effect"),0x2ea:(0,"effect"),0x61:(0,"effect"),
          0x81:(4,"effect"),0xce:(2,"effect"),0x3d:(0,"effect"),0x4e:(2,"effect"),
          0x1d:(5,"conditional_call"),0x25a:(2,"effect"),0x2ec:(2,"effect"),
          0x34b:(3,"effect"),0xd5:(6,"effect"),0x34c:(3,"effect"),0x4f:(0,"effect"),
          0x25b:(0,"effect"),0x2eb:(0,"effect"),0xc4:(3,"effect"),0xdc:(2,"effect"),
          0x25c:(2,"effect"),0xc3:(3,"effect"),0x63:(2,"effect"),0x52:(0,"effect"),
          0x26a:(2,"effect"),0x21:(2,"effect"),0x65:(2,"effect"),0x6a:(6,"effect"),
          0x4a:(2,"effect"),0x182:(2,"effect"),0x91:(1,"effect"),0x125:(0,"effect"),
          0x2d9:(2,"effect"),0x133:(9,"effect"),0xae:(8,"effect"),0x136:(1,"effect"),
          0xaf:(0,"effect"),0x134:(1,"effect"),0x267:(2,"effect"),0x137:(1,"effect"),
          0x135:(1,"effect")}


def script_audit(arm9, members, event_view, init_raw=None, item_runtime=None):
    count = struct.unpack_from("<I",arm9,0xfac90)[0]
    require(count == 853 and struct.unpack_from("<I",arm9,0x400e4)[0] == 0x020fad00,
            "Native script dispatch differs")
    ranges = list(struct.iter_unpack("<3H",span(arm9,0xfa4a4,180)))
    tables = {m:entry_table(raw) for m,raw in members.items()}
    def dispatch(sid):
        row = next((r for r in ranges if sid >= r[0]),None)
        member,index = (row[1],sid-row[0]) if row else ((850,sid-1) if sid else (140,0))
        require(member in tables and index < len(tables[member][0]), "Unresolved script archive or entry")
        return member,index,tables[member][0][index]
    def handler(op):
        return struct.unpack_from("<I",arm9,0xfad00+op*4)[0] if op < count else None
    roots = [{"member":850,"entry_index":i,"offset":offset,"origin":"conservative local table root"}
             for i,offset in enumerate(tables[850][0])]
    dispatches = []
    for kind in ("npcs","triggers","backgrounds"):
        for e in event_view[kind]:
            member,index,offset = dispatch(e["script"])
            dispatches.append({"kind":kind,"id":e["id"],"script_id":e["script"],
                               "member":member,"entry_index":index,"offset":offset})
            roots.append({**dispatches[-1],"origin":"event record"})
    pending = deque((r["member"],r["offset"]) for r in roots)
    decoded, frontiers, movements, edges, occupied = {}, [], [], [], {}
    call_returns = set()
    while pending:
        member,offset = pending.popleft()
        if (member,offset) in decoded:
            continue
        require(len(decoded) < 1024, "Bounded script trace exceeded")
        raw = members[member]; op = struct.unpack_from("<H",span(raw,offset,2))[0]
        rec = {"member":member,"offset":offset,"opcode":op,"handler_address":handler(op)}
        decoded[(member,offset)] = rec
        shape = SHAPES.get(op)
        if shape is None:
            rec["before"] = span(raw,offset,2).hex()
            frontiers.append(refusal("UNKNOWN_SCRIPT_COMMAND",**rec))
            continue
        length,kind = shape; end = offset+2+length
        rec.update(before=span(raw,offset,2+length).hex(),kind=kind)
        for byte in range(offset,end):
            require((member,byte) not in occupied or occupied[(member,byte)] == offset,
                    "Script targets overlap operands", "SCRIPT_CONTROL_FLOW_CONFLICT")
            occupied[(member,byte)] = offset
        successors = []
        if kind not in ("end","jump","return"):
            successors.append((member,end,"potential continuation"))
        if kind in ("branch","conditional_call"):
            target = end+struct.unpack_from("<i",raw,offset+3)[0]
            successors.append((member,target,"conditional branch; both outcomes retained"))
            if kind == "conditional_call":
                call_returns.add((member,end))
        elif kind in ("jump","call"):
            target = end+struct.unpack_from("<i",raw,offset+2)[0]
            successors.append((member,target,"local "+kind))
            if kind == "call":
                call_returns.add((member,end))
        elif kind == "dispatch":
            sid = struct.unpack_from("<H",raw,offset+2)[0]
            child,index,target = dispatch(sid)
            rec["script_id"] = sid
            rec["dispatched_entry"] = {"member":child,"entry_index":index,"offset":target}
            successors.append((child,target,"dispatched child"))
        elif kind == "movement":
            actor,relative = struct.unpack_from("<Hi",raw,offset+2)
            target = end+relative
            movements.append({"member":member,"command_offset":offset,"actor_operand":actor,
                              "stream_offset":target,"prefix_before":span(raw,target,8).hex(),
                              "first_opcode":struct.unpack_from("<H",raw,target)[0]})
            movements[-1]["actor_reference"] = actor_reference(actor)
        if op in COMMAND_CONTRACTS:
            contract,effect,condition = COMMAND_CONTRACTS[op]
            frontiers.append(refusal('RUNTIME_STATE_PRECONDITION',**rec,contract=contract,
                                    effect=effect,remaining_condition=condition))
        elif kind in ("effect","branch","dispatch","movement","child_release"):
            frontiers.append(refusal("UNAUDITED_TRANSITIVE_EFFECT",**rec))
        for child,target,edge in successors:
            require(tables[child][1] <= target < len(members[child])-1, "Script successor outside code")
            edges.append({"from":[member,offset],"to":[child,target],"kind":edge})
            pending.append((child,target))
    # A later-decoded command may reveal an earlier unknown target inside operands.
    require(all(occupied.get(key,key[1]) == key[1] for key in decoded),
            "Script target enters an operand", "SCRIPT_CONTROL_FLOW_CONFLICT")
    init = initialization(init_raw) if init_raw is not None else None
    if init:
        for record in init['records']:
            member,index,offset = dispatch(record['script_id'])
            record['dispatched_entry'] = {'member':member,'entry_index':index,'offset':offset}
            require((member,offset) in decoded, 'Initializer not included among script roots')
    else:
        frontiers.append(refusal('MAP_INITIALIZATION_NOT_SUPPLIED'))
    actions = movement_definitions(arm9)
    streams = []
    for member,target in sorted({(r['member'],r['stream_offset']) for r in movements}):
        stream = movement_stream(members[member],target,actions)
        require(not any((member,b) in occupied or (member,b) in decoded
                        for b in range(target,stream['end_offset'])),
                'Movement stream overlaps script instructions', 'SCRIPT_CONTROL_FLOW_CONFLICT')
        streams.append({'member':member,**stream})
    child_contract = child_serialization({'decoded':list(decoded.values())})
    safe_children = {(r['member'],r['offset']) for r in child_contract['roots'] if r['serializable']}
    safe_releases = {(r['member'],offset) for r in child_contract['roots'] if r['serializable']
                     for offset,_ in r['release_end_pairs']}
    frontiers = [f for f in frontiers if not (f['code']=='UNAUDITED_TRANSITIVE_EFFECT' and
        ((f.get('kind')=='dispatch' and (f['dispatched_entry']['member'],f['dispatched_entry']['offset']) in safe_children)
         or (f.get('kind')=='child_release' and (f['member'],f['offset']) in safe_releases)))]
    projection = project_event_routes({'decoded':list(decoded.values()),'event_dispatch':dispatches,
                                      'item_runtime':item_runtime or {}},
                                        streams,event_view,arm9)
    frontiers.extend(projection['refusals'])
    frontiers.append(refusal('DYNAMIC_EFFECT_CLOSURE_INCOMPLETE',
        detail='Explicit stream displacements and wait bookkeeping decoded; projected coordinates assume native effects. '
               'Follower entry/restored state, normal queue and transitive effects remain open. '
               'Child early-End paths require the recorded item-input contracts. SCRIPT 63 resumes; 65 removes. '
               'SCRIPT 2ea/2eb select field UI modes; full UI/overlay lifetime effects remain open.'))
    return {"complete":False,"roots":roots,"event_dispatch":dispatches,
            "decoded":sorted(decoded.values(),key=lambda r:(r["member"],r["offset"])),
            "edges":edges,"movement_references":movements,"refusals":frontiers,
            "potential_call_returns":[list(v) for v in sorted(call_returns)],
            'structural_complete':not any(f['code']=='UNKNOWN_SCRIPT_COMMAND' for f in frontiers),
            'initialization':init,'movement_actions':list(actions.values()),'movement_streams':streams,
            'child_serialization':child_contract,'runtime_contract_sites':contract_sites({'decoded':list(decoded.values())}),
            'item_runtime':item_runtime or {},
            'event_projection':projection,
            "scope":"Rooted structural graph and native movement stream decoding. Conditional coordinate projections are not dynamic safety evidence."}


def components(cells):
    unseen = set(cells); groups = []
    while unseen:
        pending = [min(unseen)]; unseen.remove(pending[0]); group = set(pending)
        while pending:
            x,z = pending.pop()
            for c in ((x-1,z),(x+1,z),(x,z-1),(x,z+1)):
                if c in unseen:
                    unseen.remove(c); group.add(c); pending.append(c)
        groups.append(group)
    return groups


def connection_check(before, after):
    """Preserve every baseline connection between surviving walkable tiles.
    This is a height-qualified static tile graph, not dynamic route execution.
    """
    new = components(after); index = {c:i for i,g in enumerate(new) for c in g}; splits = []
    for group in components(before):
        retained = group & after
        ids = sorted({index[c] for c in retained})
        if len(ids) > 1:
            splits.append([list(min(retained & new[i])) for i in ids])
    return {"preserved":not splits,"split_witnesses":splits,
            "before_component_sizes":sorted(map(len,components(before))),
            "after_component_sizes":sorted(map(len,new)),"surviving_terminals":len(before & after)}


def compose_cells(before, sources, destinations):
    """Pure hypothetical flag composition. Retained/shared owners always win.
    Unknown ownership is reserved AND separately refuses the edit. No ROM patch
    list is returned. Only core.Project may eventually persist a qualified move.
    """
    values = dict(before); owners = {}; failures = []
    for key,anchor in destinations.items():
        require(key in sources, "Unlisted source", "UNSUPPORTED_EDIT")
        for c in footprint(anchor):
            require(c in values, "Unlisted destination cell", "UNQUALIFIED_TARGET")
            if c in owners:
                failures.append(refusal("DECORATION_OVERLAP",cell=list(c),owners=[owners[c],key]))
            owners[c] = key
    for key in destinations:
        for c in sources[key]["cells"]:
            cell = tuple(c["cell"])
            if c["clear_allowed"] and not c["retained_owners"]:
                require(c["exclusive_owner"] == key, "Unproven exclusive owner", "OWNERSHIP_AMBIGUOUS")
                kind,flags = bytes.fromhex(values[cell]); values[cell] = bytes((kind,flags & ~128)).hex()
    for cell,key in owners.items():
        kind,flags = bytes.fromhex(values[cell])
        if flags & 128:
            # A retained owner, or an unmoved source, still owns this cell.
            failures.append(refusal("RETAINED_COLLISION_CONFLICT",cell=list(cell),object=key))
        if kind != 0 or flags & ~128:
            failures.append(refusal("NON_NEUTRAL_DESTINATION",cell=list(cell),before=values[cell]))
        values[cell] = bytes((kind,flags | 128)).hex()
    return values,failures


def build_definition(project):
    require(digest(project.blob) == BASELINE, "Qualification baseline changed", "BASELINE_CHANGED")
    refs = {}; resources = {}
    for archive,member in [(MAP,m) for m in (5,8,296)] + [(SCRIPTS,m) for m in (850,623,3,145,140)] + [
            ("a/0/3/2",64),("a/0/4/0",37),("a/0/4/0",38),("a/0/4/0",52)]:
        offset,raw = resource(project.blob,archive,member)
        resources[(archive,member)] = raw
        refs[f"{archive}:{member}"] = {"archive":archive,"member":member,"rom_offset":offset,
                                      "size":len(raw),"sha256":digest(raw)}
    maps = {m:resources[(MAP,m)] for m in (5,8,296)}
    raw = maps[5]; height = terrain(raw); sources = ownership(maps)
    start,_,ram,size = struct.unpack_from("<4I",project.blob,0x20)
    arm9 = ndspy.codeCompression.decompress(span(project.blob,start,size))
    base_view = events(resources[("a/0/3/2",64)])
    current_view = copy.deepcopy(base_view)
    for npc in current_view["npcs"]:
        npc.update(project.doc["positions"].get(str(npc["id"]),{}))
    protected = interaction_sets(base_view)+interaction_sets(current_view)
    script = script_audit(arm9,{m:resources[(SCRIPTS,m)] for m in (850,3,145,140)},base_view,
                          resources[(SCRIPTS,623)],item_runtime_inputs(project.blob,arm9))
    script['native_evidence'] = native_evidence(project.blob,arm9,SHAPES,
                                               {r['opcode'] for r in script['decoded']})
    script['runtime_evidence'] = runtime_evidence(project.blob,arm9)
    script['current_event_projection'] = project_event_routes(
        script,script['movement_streams'],current_view,arm9)
    # All geometry/index checks across the entire east member form the bounded
    # static graph. Only level-1 neutral walkable cells enter that graph.
    heights = {(x,z):height_coverage(height,(x,z)) for x in range(544,576) for z in range(384,416)}
    before = {(x,z):permission_cell(raw,(x,z))["before"] for x in range(544,576) for z in range(384,416)}
    anchors = []
    for anchor in ANCHORS:
        cells = footprint(anchor)
        conflicts = [{"kind":r["kind"],"id":r["id"],"cells":sorted(cells & set(map(tuple,r["cells"])))}
                     for r in protected if cells & set(map(tuple,r["cells"]))]
        anchors.append({"anchor":list(anchor),"cells":[permission_cell(raw,c) for c in sorted(cells)],
                        "height":[heights[c] for c in sorted(cells)],"static_event_conflicts":conflicts,
                        "neutral":all(before[c] == "0000" for c in cells),"write_qualified":False})
    for s in sources.values():
        s["height"] = [heights[tuple(c["cell"])] for c in s["cells"]]
        s["static_event_conflicts"] = [{"kind":r["kind"],"id":r["id"]} for r in protected
                                      if footprint(s["anchor"]) & set(map(tuple,r["cells"]))]
    slices = [(0x3fd18,0x34),(0x3fd60,8),(0x3fd6c,0x6c),(0x3fe0c,0x68),
              (0x400b0,0x38),(0x40114,0x78),(0x40340,0x18),(0xfa4a4,180),(0xfac90,4)]
    for op in sorted({r["opcode"] for r in script["decoded"]}):
        ptr = struct.unpack_from("<I",arm9,0xfad00+op*4)[0]
        slices.append((0xfad00+op*4,4))
        offset = (ptr & ~1)-ram
        if 0 <= offset < len(arm9)-112:
            slices.append((offset,112 if op in (0x14,0x5e) else 48))
    deps = {"baseline_sha256":BASELINE,"project_revision":project.doc["revision"],
            "project_sha256":digest(project.path.read_bytes()),"history_entries":len(project.doc["history"]),
            "authored_positions":project.doc["positions"],"authored_placement_moves":project.doc["placement_moves"],
            "resources":refs,"native_evidence_sha256":canonical_hash(script['native_evidence']),
            "runtime_evidence_sha256":canonical_hash(script['runtime_evidence']),
            "item_runtime_sha256":canonical_hash(script['item_runtime']),
            "arm9":{"rom_offset":start,"ram_address":ram,"compressed_size":size,
                "decompressed_sha256":digest(arm9),"slices":[{"offset":o,"size":n,"before":span(arm9,o,n).hex(),
                    "sha256":digest(span(arm9,o,n))} for o,n in slices]}}
    current = dict(before)
    for key,move in project.doc["placement_moves"].items():
        require(key == "5:14", "Unexpected authored placement", "UNSUPPORTED_EDIT")
        # Project has already checked the exact M3/M4 record and dependencies.
        for c in footprint([move["before"]["x"],move["before"]["z"]]):
            kind,flags = bytes.fromhex(current[c]); current[c] = bytes((kind,flags & ~128)).hex()
        for c in footprint([move["after"]["x"],move["after"]["z"]]):
            kind,flags = bytes.fromhex(current[c]); current[c] = bytes((kind,flags | 128)).hex()
    walkable = [list(c) for c,h in heights.items() if h["qualified"] and before[c] in ("0000","0004")]
    current_walkable = [list(c) for c,h in heights.items() if h["qualified"] and current[c] in ("0000","0004")]
    return {"schema":SCHEMA,"id":"cherrygrove-three-planter-prerequisite-v3","write_qualified":False,
            "dependencies":deps,"dependencies_sha256":canonical_hash(deps),"sources":sources,
            "terrain":height,"anchors":anchors,"static_events":{"baseline":base_view,"current":current_view},
            "scripts":script,"static_graph":{"member":5,"bounds":[544,384,576,416],
                "policy":"level-1 closed whole tiles; behavior 00, flags 00/04; cardinal edges; every surviving baseline connection",
                "walkable":walkable,"current_walkable":current_walkable,
                "permissions_hex":map_data(raw)[0].hex(),
                "current_permissions_hex":"".join(current[(x,z)] for z in range(384,416) for x in range(544,576))},
            "proposed_layout":PROPOSAL}


def assess_layout(definition, targets):
    """Explain a candidate; callers must not interpret static success as a grant."""
    require(definition["schema"] == SCHEMA, "Unknown qualification schema")
    require(isinstance(targets,dict) and set(targets) == set(definition["sources"]),
            "Specify the entire three-object layout", "INCOMPLETE_LAYOUT")
    failures = []; source_cells = set()
    for key,s in definition["sources"].items():
        footprint(targets[key])
        source_cells.update(tuple(c["cell"]) for c in s["cells"])
        failures.extend(s["refusals"])
        if not all(h["qualified"] for h in s["height"]):
            failures.append(refusal("SOURCE_HEIGHT_UNQUALIFIED",object=key))
        if s["static_event_conflicts"]:
            failures.append(refusal("SOURCE_EVENT_CONFLICT",object=key))
        a = next((a for a in definition["anchors"] if a["anchor"] == list(targets[key])),None)
        require(a is not None, "Target outside 23 bounded candidates", "UNQUALIFIED_TARGET")
        if not a["neutral"]:
            failures.append(refusal("NON_NEUTRAL_DESTINATION",object=key))
        if not all(h["qualified"] for h in a["height"]):
            failures.append(refusal("DESTINATION_HEIGHT_UNQUALIFIED",object=key))
        if a["static_event_conflicts"]:
            failures.append(refusal("DESTINATION_EVENT_CONFLICT",object=key,conflicts=a["static_event_conflicts"]))
    permissions = bytes.fromhex(definition["static_graph"]["permissions_hex"])
    before = {(544+i%32,384+i//32):permissions[2*i:2*i+2].hex() for i in range(1024)}
    composed,conflicts = compose_cells(before,definition["sources"],targets); failures.extend(conflicts)
    walkable = set(map(tuple,definition["static_graph"]["walkable"]))
    eligible = walkable | {c for c in source_cells if all(h["qualified"] for s in definition["sources"].values()
                                                                    for h in s["height"] if tuple(h["cell"]) == c)}
    after = {c for c in eligible if composed[c] in ("0000","0004")}
    connections = connection_check(walkable,after)
    current_connections = connection_check(set(map(tuple,definition["static_graph"]["current_walkable"])),after)
    if not connections["preserved"]:
        failures.append(refusal("WALKING_CONNECTION_SPLIT",witnesses=connections["split_witnesses"]))
    if not current_connections["preserved"]:
        failures.append(refusal("CURRENT_WALKING_CONNECTION_SPLIT",witnesses=current_connections["split_witnesses"]))
    projected_conflicts = []
    for state,field in (('baseline','event_projection'),('revision5','current_event_projection')):
        for key,anchor in targets.items():
            for route in definition['scripts'][field]['launches']:
                cells = footprint(anchor) & set(map(tuple,route['swept_tiles']))
                if cells:
                    projected_conflicts.append({'object':key,'event_state':state,
                        'cells':[list(c) for c in sorted(cells)],
                        **{k:route[k] for k in ('scenario','member','command_offset','stream_offset','actor')}})
    if projected_conflicts:
        failures.append(refusal('PROJECTED_SCRIPT_ROUTE_CONFLICT',witnesses=projected_conflicts,
            scope='Conditional coordinate projection; conservative refusal, not a claim of executed movement.'))
    # Explicitly cannot be overridden by deleting frontiers or editing JSON booleans.
    failures.append(refusal("SCRIPT_PROOF_INCOMPLETE",frontier_count=len(definition["scripts"]["refusals"])))
    return {"layout":targets,"qualified":False,"refusals":failures,
            "static_collision_conflicts":conflicts,"static_connections":connections,
            "current_static_connections":current_connections,
            'projected_script_conflicts':projected_conflicts,
            "hypothetical_changed_flag_cells":[list(c) for c in sorted(before) if before[c] != composed[c]],
            "retained_ambiguous_cells":[c["cell"] for s in definition["sources"].values() for c in s["cells"] if c["retained_owners"]],
            "scope":"Static hypothetical composition only. No writes, ROM patches or movement grant."}


def require_layout(project, definition, targets, expected_revision):
    """Future core integration boundary: re-read, rebuild, compare; always refuse
    while this prerequisite's native semantics remain incomplete. No mutation.
    """
    from .core import Project
    fresh = Project(project.root)
    require(type(expected_revision) is int and fresh.doc["revision"] == expected_revision,
            "Project revision changed", "STALE_REVISION")
    actual = build_definition(fresh)
    require(canonical_hash(actual) == canonical_hash(definition),
            "Qualification before-values or dependencies changed", "QUALIFICATION_CHANGED")
    result = assess_layout(actual,targets)
    require(result["qualified"], "M5 layout refused: "+", ".join(sorted({r["code"] for r in result["refusals"]})),
            "UNQUALIFIED_LAYOUT")
    return result
