"""Export composed object tables through Project's locked write operation."""
import json
import shutil
import struct

import numpy as np

from . import authoring, containers, scenery, world, event_authoring, surface_authoring, simple_interactions, story_authoring, character_runtime
from .formats import digest, file_span, map_sections, map_data, member_span, resource, require


def export(project, output, save):
    state = project.composed()
    contexts = {c["map_member"]: c for c in state["contexts"]}
    if project.doc["placement_moves"]:
        contexts[5] = project.context(header=67, cell=[17, 12])
    replacements, map_report = {}, []
    for member, context in sorted(contexts.items()):
        raw = project.member_raw(member)
        sections = map_sections(raw)
        table = scenery.table_for(project, context, state)
        records = b"".join(obj["raw"] for obj in table.values())
        model = surface_authoring.model(project, context, state)
        result = bytearray(raw[:sections["buildings_offset"]] + records + model + raw[sections["terrain_offset"]:])
        struct.pack_into("<I", result, 8, len(model))
        struct.pack_into("<I", result, 4, len(records))
        for (m, offset), value in state["permissions"].items():
            if m == member:
                result[offset:offset + 2] = value
        current = map_sections(result)
        require(result[16:sections["permissions_offset"]] == raw[16:sections["permissions_offset"]]
                and result[current["terrain_offset"]:] == raw[sections["terrain_offset"]:],
                "BGS or BDHC changed")
        require(len(map_data(result)[1]) == len(table), "Object table readback differs")
        if result != raw:
            replacements[member] = bytes(result)
            map_report.append({"member": member, "before_count": sections["building_count"],
                               "after_count": len(table), "before_bytes": len(raw), "after_bytes": len(result),
                               "export_slots": [{"slot": i, "authoring_slot": slot, "id": obj["id"]}
                                                for i, (slot, obj) in enumerate(table.items())],
                               "preserved_bgs_model_bdhc": model == map_data(raw)[2], "preserved_bgs_bdhc": True,
                               "model_before_sha256": digest(map_data(raw)[2]), "model_after_sha256": digest(model)})
    data = bytearray(project.blob)
    patches = []
    def patch(offset, before, after, kind):
        require(len(before) == len(after) and data[offset:offset + len(before)] == before,
                "Event export before-value differs", "BEFORE_VALUE_MISMATCH")
        if before != after:
            data[offset:offset + len(after)] = after
            patches.append({"rom_offset": offset, "before": before.hex(), "after": after.hex(), "kind": kind})
    for change in event_authoring.final_patches(project, state):
        patch(change['rom_offset'], bytes.fromhex(change['before']), bytes.fromhex(change['after']), change['kind'])
    arm9_start = struct.unpack_from('<I', project.blob, 0x20)[0]
    for header, transaction in sorted(project._room_headers.items()):
        offset = world.HEADER_TABLE + header * world.HEADER_SIZE + 4
        patch(arm9_start + offset, project._base_arm9[offset:offset + 2],
              project.arm9[offset:offset + 2], 'interior.matrix-reference')
    runtime = story_authoring.runtime(project, state)
    for change in runtime['patches']:
        patch(change['rom_offset'], bytes.fromhex(change['before']), bytes.fromhex(change['after']), change['kind'])
    structural_events = {v['event_member'] for v in list(simple_interactions.specs(state).values()) + list(story_authoring.catalog(state, 'sequence').values())}
    for member in {m for m, _ in state['event_records']} - structural_events:
        require(resource(data, world.EVENT_ARCHIVE, member)[1] == event_authoring.raw_member(project, member, state),
                "Composed event readback differs")
    _, archive = file_span(data, world.MAP_ARCHIVE)
    expanded_archive = containers.append_members(archive, [v for _, v in sorted(project._room_members.items())])
    changed_archive, members = containers.replace_members(expanded_archive, replacements)
    result, allocation = containers.replace_file(data, world.MAP_ARCHIVE, changed_archive)
    expected_files = {file_span(project.blob, world.MAP_ARCHIVE)[0]: changed_archive}
    extra_archives = story_authoring.replacements(project, state)
    for name in runtime['appends']: extra_archives.setdefault(name, {})
    extra_archives[world.EVENT_ARCHIVE] = {m: event_authoring.raw_member(project, m, state) for m in structural_events}
    if project._room_matrices:
        extra_archives[world.MATRIX_ARCHIVE] = {}
    extra_report = []
    for name, changes in sorted(extra_archives.items()):
        appended = [v for _, v in sorted(project._room_matrices.items())] if name == world.MATRIX_ARCHIVE else runtime['appends'].get(name, [])
        if not changes and not appended:
            continue
        old_start, old_archive = file_span(data, name)
        expanded = containers.append_members(old_archive, appended)
        new_archive, member_report = containers.replace_members(expanded, changes)
        result, relocated = containers.replace_file(result, name, new_archive)
        expected_files[old_start] = new_archive
        extra_report.append({'archive': name, 'members': member_report, 'appended_members': len(appended), 'allocation': relocated})
    runtime_report = []
    for file_id, payload in sorted(runtime['files'].items()):
        old_start, _ = character_runtime.file_by_id(project.blob, file_id)
        result, relocated = containers.replace_file_id(result, file_id, payload)
        expected_files[old_start] = payload
        runtime_report.append({'file_id':file_id, 'sha256':digest(payload), 'allocation':relocated})
    # Check ALL ROM file payloads using the exported allocation table, and every
    # map member using its exported table. Event changes were explicit fixed patches.
    fat, fat_size = struct.unpack_from("<II", project.blob, 0x48)
    old_files = list(struct.iter_unpack("<II", project.blob[fat:fat + fat_size]))
    new_files = list(struct.iter_unpack("<II", result[fat:fat + fat_size]))
    map_start, _ = file_span(project.blob, world.MAP_ARCHIVE)
    checked = 0
    for (a, b), (c, d) in zip(old_files, new_files):
        require(0 <= c <= d <= len(result), "Export file allocation exceeds ROM")
        if a in expected_files:
            require(result[c:d] == expected_files[a], "Export authored archive differs")
        else:
            require(result[c:d] == data[a:b], "Unrelated ROM file payload differs")
            checked += 1
    _, readback = file_span(result, world.MAP_ARCHIVE)
    count = struct.unpack_from("<H", expanded_archive, 24)[0]
    for i in range(count):
        require(member_span(readback, i)[1] == replacements.get(i, member_span(expanded_archive, i)[1]),
                "Export map member differs")
    changed_count = abs(len(result) - len(project.blob))
    for offset in range(0, len(project.blob), 1024 * 1024):
        count_bytes = min(1024 * 1024, len(project.blob) - offset)
        changed_count += int(np.count_nonzero(np.frombuffer(result, dtype=np.uint8, count=count_bytes, offset=offset)
                            != np.frombuffer(project.blob, dtype=np.uint8, count=count_bytes, offset=offset)))
    report = {"schema": "sovereign-editor-export-v2", "revision": project.doc["revision"],
              "baseline_sha256": digest(project.blob), "candidate_sha256": digest(result),
              "rom_bytes": len(result), "changes": project.diff(), "patches": patches,
              "character_bindings": runtime["characters"], "runtime_files": runtime_report,
              "maps": map_report, "archive_members": members, "allocation": allocation, "extra_archives": extra_report,
              "independent_interiors": [{'header': h, 'map_member': t['map_member'], 'matrix_member': t['matrix_member']}
                                        for h, t in sorted(project._room_headers.items())],
              "changed_byte_count": changed_count, "all_other_rom_bytes_equal": not allocation["relocated"] and not extra_report,
              "preservation": "Unchanged members and all unrelated ROM file payloads are exact; authored maps/events/scripts/text are independently read back. Relocation fields and appended data are audited separately.",
              "unrelated_files_verified": checked, "map_members_verified": count,
              "native_acceptance": "pending", "save_sha256": digest(save) if save else None}
    output.mkdir(parents=True, exist_ok=False)
    try:
        (output / "game.nds").write_bytes(result)
        if save is not None:
            (output / "game.sav").write_bytes(save)
        (output / "export.json").write_text(json.dumps(report, indent=2) + "\n")
        lines = ["Map and event authoring — native acceptance pending", "",
                 "Open game.nds with its adjacent ordinary game.sav. Preserve any previously played save.",
                 "Check added/removed/transferred scenery and every explicitly changed collision tile.",
                 "Check the moved New Bark sign still reads, its old spot is clear, and transitions work.",
                 "Leave/re-enter, save normally, close/reopen and load. Report pass/fail/untested.", ""]
        for change in project.diff():
            if change["operation"] == "scenery.transaction":
                lines.extend([change["label"], f"  {change['context']['id']} → {change['destination_context']['id']}",
                              f"  {change['action']}: {change['from']} → {change['to']}",
                              f"  Explicit permission cells: {len(change['permission_cells'])}"])
            elif change["operation"] == "event.transaction":
                lines.extend([change["label"], *[f"  {e['kind']} {e['member']}:{e['id']}: {e['from']} → {e['to']}"
                                               for e in change['events']]])
        (output / "PLAYTEST.txt").write_text("\n".join(lines) + "\n")
    except BaseException:
        shutil.rmtree(output)
        raise
    return {**report, "output": str(output)}
