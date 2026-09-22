"""Explicit New Bark town-sign binding; no inferred or general event ownership.

The pinned background record is independently identified in map-authoring's
baseline: event member 57, record 2, script 15 at (685,400). Only X/Z change.
All event, script and message resources are dependencies, never reconstructed.
"""
import math
import struct

from . import authoring, world
from .formats import digest, events, require, resource

SLOT = 13
EVENT_ID = 2
RECORD_OFFSET = 4 + EVENT_ID * 20
BASE_RECORD = bytes.fromhex("0f000100ad020000900100000000000004000000")


def supported(context):
    return (context["header"]["id"] == 60 and context["map_member"] == 0
            and context["matrix"]["id"] == 0 and context["origin"] == [672, 384])


def binding(project, context):
    require(supported(context), "Only the identified New Bark town-sign interaction is supported",
            "UNSUPPORTED_INTERACTION")
    head = context["header"]
    require((head["event_file"], head["script_file"], head["text_archive"]) == (57, 842, 542),
            "New Bark sign resource references changed", "UNQUALIFIED_DEPENDENCIES")
    _, raw = resource(project.blob, world.EVENT_ARCHIVE, 57)
    decoded = events(raw)
    require(len(decoded["backgrounds"]) > EVENT_ID
            and raw[RECORD_OFFSET:RECORD_OFFSET + 20] == BASE_RECORD,
            "New Bark sign baseline record changed", "UNQUALIFIED_DEPENDENCIES")
    props = project.member_data(0)[1]
    require(len(props) > SLOT and props[SLOT]["model_id"] == 29
            and authoring.global_position(context, props[SLOT]) == {"x": 685.5, "y": 1.0, "z": 400.5},
            "New Bark town-sign placement identity changed", "UNQUALIFIED_DEPENDENCIES")
    refs = [(world.EVENT_ARCHIVE, 57), ("a/0/1/2", head["script_file"]),
            ("a/0/1/2", head["level_script"]), ("a/0/2/7", head["text_archive"])]
    dependencies = [{"archive": a, "member": m, "sha256": digest(resource(project.blob, a, m)[1])}
                    for a, m in refs]
    return raw, decoded, dependencies


def plan(project, context, placement_records, interaction_records):
    raw, decoded, dependencies = binding(project, context)
    prop = project.member_data(0)[1][SLOT]
    position = authoring.global_from_record(context, placement_records.get((0, SLOT), authoring.record_state(prop)))
    # The observed sign is centered on its tile; retain that exact convention.
    require(position["x"] % 1 == .5 and position["z"] % 1 == .5,
            "Town-sign interaction alignment requires a tile-centered (.5) anchor", "UNQUALIFIED_TARGET")
    x, z = math.floor(position["x"]), math.floor(position["z"])
    world.cell_offset(context, x, z)
    for event in decoded["backgrounds"]:
        require(event["id"] == EVENT_ID or (event["x"], event["z"]) != (x, z),
                "Another background interaction already occupies the target tile", "EVENT_CONFLICT")
    before = interaction_records.get((57, EVENT_ID), raw[RECORD_OFFSET:RECORD_OFFSET + 20])
    after = bytearray(before)
    struct.pack_into("<2i", after, 4, x, z)
    if after == before:
        return None
    return {"binding": "new-bark-town-sign-v1", "event_member": 57, "event_id": EVENT_ID,
            "placement_slot": SLOT, "record_offset": RECORD_OFFSET,
            "before": before.hex(), "after": bytes(after).hex(),
            "from": list(struct.unpack_from("<2i", before, 4)), "to": [x, z],
            "script": 15, "dependencies": dependencies}


def patches(project, change):
    base, _ = resource(project.blob, world.EVENT_ARCHIVE, change["event_member"])
    before, after = bytes.fromhex(change["before"]), bytes.fromhex(change["after"])
    return [{"kind": f"sign-interaction.{axis}", "event_member": change["event_member"],
             "event_id": change["event_id"], "rom_offset": base + change["record_offset"] + offset,
             "before": before[offset:offset + 4].hex(), "after": after[offset:offset + 4].hex()}
            for axis, offset in (("x", 4), ("z", 8)) if before[offset:offset + 4] != after[offset:offset + 4]]
