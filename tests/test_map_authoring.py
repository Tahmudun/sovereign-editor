"""General map authoring: two real map contexts, explicit permissions, export bytes.

Every check runs against the copied baseline ROM in this workspace. Export readback
extracts the archive with ndspy and then decodes the map member with ``struct``
directly from the DSPRE ``MapFile.cs`` layout, so the expected offsets and values are
computed independently of the product readers that produced them.
"""
import copy
import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from sovereign_editor import authoring, world
from sovereign_editor.core import Project, atomic_json
from sovereign_editor.formats import EditorError, digest, resource

# Two distinct map contexts of the pinned ROM, resolved through header + matrix cell.
CHERRYGROVE = {"header": 67, "cell": [17, 12], "member": 5, "origin": [544, 384]}
NEW_BARK = {"header": 60, "cell": [21, 12], "member": 0, "origin": [672, 384]}
# Cherrygrove west planter (model 52) and its three stock blocked cells.
CG_SLOT, CG_FROM, CG_TO = 12, {"x": 550.5, "y": 1, "z": 398.5}, {"x": 551.5, "y": 1, "z": 398.5}
CG_CELLS = [{"x": 550, "z": z} for z in (397, 398, 399)]
# New Bark single-cell object (model 29) west of the lab.
NB_SLOT, NB_FROM, NB_TO = 13, {"x": 685.5, "y": 1, "z": 400.5}, {"x": 684.5, "y": 1, "z": 400.5}
NB_CELLS = [{"x": 685, "z": 400}]
# A stock record with a non-halving fraction, used for exact 16.16 handling.
NB_FRACTIONAL_SLOT = 1


@pytest.fixture(scope="module")
def workspace(tmp_path_factory):
    source = Path(os.environ.get("SG_TEST_ROM", "projects/cherrygrove/baseline.nds"))
    if not source.is_file():
        pytest.skip("Set SG_TEST_ROM to a qualified local HeartGold ROM")
    root = tmp_path_factory.mktemp("map-authoring") / "project"
    project = Project.create(source, root)
    return project.root, copy.deepcopy(project.doc)


@pytest.fixture
def project(workspace):
    root, original = workspace
    atomic_json(root / "project.json", original)
    yield Project(root)
    atomic_json(root / "project.json", original)


# ---- independent struct decoder (no product parsing) -------------------------


def decode_member(raw):
    """MapFile.cs land_data layout, decoded here so expectations stay independent."""
    permissions, buildings, model, terrain = struct.unpack_from("<4I", raw, 0)
    signature, bgs_length = struct.unpack_from("<HH", raw, 16)
    assert signature == 0x1234 and permissions == 2048 and buildings % 48 == 0
    perm = 20 + bgs_length
    build = perm + permissions
    mdl = build + buildings
    bdhc = mdl + model
    assert bdhc + terrain == len(raw)
    records = []
    for slot in range(buildings // 48):
        base = build + slot * 48
        model_id, = struct.unpack_from("<I", raw, base)
        x, y, z = struct.unpack_from("<3i", raw, base + 4)
        records.append({"slot": slot, "offset": base, "model_id": model_id, "x": x, "y": y, "z": z})
    return {"bgs": (16, 4 + bgs_length), "permissions": perm, "permission_bytes": permissions,
            "buildings": build, "building_bytes": buildings, "model": mdl, "model_bytes": model,
            "terrain": bdhc, "terrain_bytes": terrain, "records": records}


def cell_byte(decoded, origin, x, z):
    return decoded["permissions"] + 2 * ((z - origin[1]) * 32 + (x - origin[0]))


def word(origin_axis, value):
    """Global tile anchor -> the 16.16 record word, computed independently."""
    scaled = (value - origin_axis - 16) * 65536
    assert float(scaled).is_integer()
    return int(scaled)


def member_bytes(blob, member):
    return resource(blob, world.MAP_ARCHIVE, member)[1]


def narc_member(rom_bytes, archive, member):
    import ndspy.narc
    import ndspy.rom
    return ndspy.narc.NARC(ndspy.rom.NintendoDSRom(rom_bytes).getFileByName(archive)).files[member]


# ---- context resolution ------------------------------------------------------


def test_contexts_resolve_without_enumerating_every_destination(project):
    matrices = project.contexts(limit=3)
    assert matrices["kind"] == "matrices" and matrices["total"] == 288
    assert matrices["matrices"][0]["matrix"] == 0 and matrices["matrices"][0]["has_headers"]
    listing = project.contexts(matrix=0, map_member=NEW_BARK["member"])
    assert listing["kind"] == "cells" and listing["total"] == 1
    assert listing["cells"][0]["cell"] == NEW_BARK["cell"]
    assert listing["cells"][0]["header"] == NEW_BARK["header"]
    # Header lookup does not require walking the whole header table.
    by_header = project.contexts(header=CHERRYGROVE["header"])
    assert [c["cell"] for c in by_header["cells"]] == [[16, 12], [17, 12]]
    assert {c["map_member"] for c in by_header["cells"]} == {4, 5}


def test_context_listing_pages_and_searches_beyond_one_page(project):
    everything = project.contexts(matrix=0, limit=400)
    assert everything["total"] > 400 and everything["returned"] == 400
    tail = project.contexts(matrix=0, limit=400, offset=400)
    assert tail["returned"] == everything["total"] - 400 and tail["offset"] == 400
    assert tail["cells"][0] not in everything["cells"]
    # A context beyond the first page is still reachable by name.
    found = project.contexts(matrix=0, search="T20")
    assert [c["cell"] for c in found["cells"]] == [NEW_BARK["cell"]]
    # A numeric term also matches a map member number.
    by_member = project.contexts(matrix=0, search="111")
    assert any(c["map_member"] == 111 for c in by_member["cells"])
    assert all(c["map_member"] == 111 or "111" in c["name"] for c in by_member["cells"])
    assert project.contexts(matrix=0, search="no-such-map")["total"] == 0


def headerless_context(project):
    """A real header whose own matrix carries no header section (indoor family)."""
    headerless = {m["matrix"] for m in project.contexts(limit=400)["matrices"]
                  if not m["has_headers"] and m["populated_cells"]}
    for header_id in range(world.header_count(project.blob)):
        header = world.read_header(project.blob, header_id, project.arm9)
        if header["matrix"] not in headerless:
            continue
        cells = project.contexts(matrix=header["matrix"])["cells"]
        try:
            view = project.map_view(header=header_id, cell=cells[0]["cell"])
        except EditorError:
            continue
        return header_id, cells[0]["cell"], view
    pytest.skip("this ROM has no resolvable headerless-matrix context")


def test_headerless_matrix_listing_keeps_its_cells(project):
    headerless = next(m["matrix"] for m in project.contexts(limit=400)["matrices"]
                      if not m["has_headers"] and m["populated_cells"])
    listing = project.contexts(matrix=headerless, header=CHERRYGROVE["header"])
    assert listing["total"] > 0 and not listing["has_headers"]
    assert "explicit --header" in listing["header_required"]
    assert all(c["header"] is None for c in listing["cells"])
    # The matrix alone cannot resolve a cell without a header section.
    with pytest.raises(EditorError) as error:
        project.context(matrix=headerless, cell=listing["cells"][0]["cell"])
    assert error.value.code == "CONTEXT_AMBIGUOUS"


def test_a_real_headerless_context_resolves_and_lists_its_resources(project):
    header_id, cell, view = headerless_context(project)
    context = project.context(header=header_id, cell=cell)
    assert context["header"]["id"] == header_id and not context["matrix"]["has_headers"]
    decoded = decode_member(member_bytes(project.blob, context["map_member"]))
    assert view["sections"]["permissions_offset"] == decoded["permissions"]
    assert len(view["placements"]) == len(decoded["records"])
    assert len(view["permissions"]["rows"]) == 32
    assert view["area_data"]["area_type_name"] in ("indoor", "outdoor")


def test_native_inspector_opens_a_headerless_context_by_header(project):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from sovereign_editor.map_inspector import MapInspectorWindow
    app = QApplication.instance() or QApplication([])
    header_id, cell, expected = headerless_context(project)
    window = MapInspectorWindow(project.root, context=(NEW_BARK["header"], NEW_BARK["cell"]))
    app.processEvents()
    window.header_input.setValue(header_id)
    window.open_header()
    app.processEvents()
    assert window.view_data["context"]["id"] == expected["context"]["id"]
    assert window.matrix_input.value() == window.view_data["header"]["matrix"]
    # The list shows the cells even though they carry no per-cell header.
    assert window.cell_list.count() >= 1
    assert "(no header section)" in window.cell_list.item(0).text()
    # Selecting a listed cell falls back to the header input, so it still resolves.
    window.cell_list.setCurrentRow(0)
    app.processEvents()
    assert window.view_data["context"]["map_member"] == expected["context"]["map_member"]
    window.close()
    app.processEvents()


@pytest.mark.parametrize("spec", [CHERRYGROVE, NEW_BARK])
def test_context_carries_explicit_member_and_origin(project, spec):
    context = project.context(header=spec["header"], cell=spec["cell"])
    assert context["map_member"] == spec["member"] and context["origin"] == spec["origin"]
    assert context["header"]["matrix"] == 0 and context["matrix"]["id"] == 0
    raw = member_bytes(project.blob, spec["member"])
    assert context["map_sha256"] == digest(raw)
    decoded = decode_member(raw)
    assert context["sections"]["permissions_offset"] == decoded["permissions"]
    assert context["sections"]["buildings_offset"] == decoded["buildings"]
    assert context["sections"]["terrain_offset"] == decoded["terrain"]
    assert context["area_data"]["area_type_name"] == "outdoor"
    assert context["event_member"] == context["header"]["event_file"]


def test_ambiguous_and_unknown_contexts_refuse(project):
    with pytest.raises(EditorError) as error:
        project.context(header=CHERRYGROVE["header"])
    assert error.value.code == "CONTEXT_AMBIGUOUS"
    with pytest.raises(EditorError) as error:
        project.context(header=CHERRYGROVE["header"], cell=[0, 0])
    assert error.value.code == "NOT_FOUND"
    with pytest.raises(EditorError) as error:
        project.context(header=99999)
    assert error.value.code == "NOT_FOUND"
    with pytest.raises(EditorError) as error:
        project.context(header=CHERRYGROVE["header"], matrix=7, cell=CHERRYGROVE["cell"])
    assert error.value.code == "CONTEXT_MISMATCH"


@pytest.mark.parametrize("spec,slot,anchor", [(CHERRYGROVE, CG_SLOT, CG_FROM), (NEW_BARK, NB_SLOT, NB_FROM)])
def test_map_view_reports_stock_placements_and_grid(project, spec, slot, anchor):
    view = project.map_view(header=spec["header"], cell=spec["cell"])
    placement = next(p for p in view["placements"] if p["slot"] == slot)
    assert placement["position"] == anchor and not placement["changed"] and placement["editable"]
    decoded = decode_member(member_bytes(project.blob, spec["member"]))
    record = decoded["records"][slot]
    assert placement["record"] == {"x": record["x"], "y": record["y"], "z": record["z"]}
    assert placement["record_offset"] == record["offset"]
    rows = view["permissions"]["rows"]
    assert len(rows) == 32 and all(len(row) == 32 for row in rows)
    ox, oz = spec["origin"]
    assert rows[int(anchor["z"]) - oz][int(anchor["x"]) - ox][2:] == "80"
    assert view["changed_permission_cells"] == []
    assert view["events"]["member"] == view["context"]["event_member"]


# ---- one transaction: placement + explicit permission cells -------------------


def apply_move(project, spec, slot, target, cells, revision=0, **kw):
    return project.apply_map_edit(revision, header=spec["header"], cell=spec["cell"],
                                  placement={"slot": slot, "x": target["x"], "z": target["z"]},
                                  move_collision=cells, **kw)


@pytest.mark.parametrize("spec,slot,source,target,cells", [
    (CHERRYGROVE, CG_SLOT, CG_FROM, CG_TO, CG_CELLS),
    (NEW_BARK, NB_SLOT, NB_FROM, NB_TO, NB_CELLS)])
def test_move_with_selected_collision_saves_reopens_undoes_and_exports(
        project, tmp_path, spec, slot, source, target, cells):
    import ndspy.rom
    plan = project.plan_map_edit(header=spec["header"], cell=spec["cell"],
                                 placement={"slot": slot, "x": target["x"], "z": target["z"]},
                                 move_collision=cells)
    assert plan["preview"]["permission_cells_changed"] == 2 * len(cells)
    assert plan["preview"]["delta"] == [target["x"] - source["x"], 0]
    assert project.doc["revision"] == 0 and not project.doc["map_edits"]

    result = apply_move(project, spec, slot, target, cells)
    assert result["changed"] and result["revision"] == 1
    transaction = result["transaction"]
    assert transaction["schema"] == authoring.SCHEMA and transaction["index"] == 0
    assert transaction["placements"][0]["before"] == source
    assert transaction["placements"][0]["after"] == target
    moved = {(c["x"], c["z"]): (c["before"], c["after"]) for c in transaction["permissions"]}
    dx = int(target["x"] - source["x"])
    for cell in cells:
        assert moved[(cell["x"], cell["z"])][1][2:] == "00"
        assert moved[(cell["x"] + dx, cell["z"])][1][2:] == "80"

    # Reopen from disk: the composed view survives a save/reopen cycle.
    reopened = Project(project.root)
    assert reopened.doc["revision"] == 1
    view = reopened.map_view(header=spec["header"], cell=spec["cell"])
    placement = next(p for p in view["placements"] if p["slot"] == slot)
    assert placement["position"] == target and placement["changed"]
    assert {(c["x"], c["z"]) for c in view["changed_permission_cells"]} == set(moved)

    report = reopened.export(tmp_path / "edited", 1)
    edited = (tmp_path / "edited/game.nds").read_bytes()
    assert report["changed_byte_count"] == len(report["patches"])
    original = ndspy.rom.NintendoDSRom(project.blob)
    candidate = ndspy.rom.NintendoDSRom(edited)
    assert [i for i, (a, b) in enumerate(zip(original.files, candidate.files)) if a != b] \
        == [original.filenames.idOf(world.MAP_ARCHIVE)]
    stock = narc_member(project.blob, world.MAP_ARCHIVE, spec["member"])
    authored = narc_member(edited, world.MAP_ARCHIVE, spec["member"])
    assert len(stock) == len(authored)

    # Independent expectation: which member bytes must differ, and to what.
    decoded = decode_member(stock)
    ox, oz = spec["origin"]
    record = decoded["records"][slot]
    expected = {}
    for i, value in enumerate(struct.pack("<i", word(ox, target["x"]))):
        if stock[record["offset"] + 4 + i] != value:
            expected[record["offset"] + 4 + i] = value
    for (x, z), (_, after) in moved.items():
        offset = cell_byte(decoded, spec["origin"], x, z)
        for i, value in enumerate(bytes.fromhex(after)):
            if stock[offset + i] != value:
                expected[offset + i] = value
    differing = {i: authored[i] for i in range(len(stock)) if stock[i] != authored[i]}
    assert differing == expected

    # Every preserved region, byte for byte.
    after_decoded = decode_member(authored)
    assert after_decoded == {**decoded, "records": after_decoded["records"]}
    assert authored[slice(*decoded["bgs"])] == stock[slice(*decoded["bgs"])]
    assert authored[decoded["model"]:] == stock[decoded["model"]:]
    assert struct.unpack_from("<3i", authored, record["offset"] + 4)[1] == record["y"]
    assert struct.unpack_from("<3i", authored, record["offset"] + 4)[2] == record["z"]
    kept = set(range(record["offset"], record["offset"] + 48)) - set(range(record["offset"] + 4, record["offset"] + 8))
    assert all(authored[i] == stock[i] for i in kept)
    targeted = {cell_byte(decoded, spec["origin"], x, z) + i for (x, z) in moved for i in (0, 1)}
    assert all(authored[i] == stock[i]
               for i in range(decoded["permissions"], decoded["buildings"]) if i not in targeted)
    # Non-target permission bits of the touched cells, and the whole event archive.
    for (x, z), (before, after) in moved.items():
        assert bytes.fromhex(before)[0] == bytes.fromhex(after)[0]
        assert bytes.fromhex(before)[1] & ~128 == bytes.fromhex(after)[1] & ~128
    assert narc_member(edited, world.EVENT_ARCHIVE, 64) == narc_member(project.blob, world.EVENT_ARCHIVE, 64)

    reopened.undo(1)
    undone = reopened.export(tmp_path / "undone", 2)
    assert undone["candidate_sha256"] == digest(project.blob)
    assert undone["changed_byte_count"] == 0
    assert not Project(project.root).doc["map_edits"]


def test_coordinate_only_edit_retains_permissions(project):
    result = apply_move(project, NEW_BARK, NB_SLOT, NB_TO, [])
    assert result["preview"]["permission_cells_changed"] == 0
    assert "retained exactly" in result["preview"]["permissions"]
    assert result["transaction"]["permissions"] == []
    view = project.map_view(header=NEW_BARK["header"], cell=NEW_BARK["cell"])
    assert view["changed_permission_cells"] == []
    ox, oz = NEW_BARK["origin"]
    # The stock blocked cell is exactly where it was; nothing followed the model.
    assert view["permissions"]["rows"][400 - oz][685 - ox][2:] == "80"
    assert view["permissions"]["rows"][400 - oz][684 - ox][2:] == "00"


def test_noop_edit_changes_nothing(project):
    before = project.path.read_bytes()
    result = apply_move(project, NEW_BARK, NB_SLOT, NB_FROM, [])
    assert result["changed"] is False and result["revision"] == 0 and result["patch_count"] == 0
    assert result["preview"]["placements_changed"] == 0
    assert project.doc["revision"] == 0 and project.path.read_bytes() == before
    assert not Project(project.root).doc["map_edits"]


def test_explicit_permission_only_transaction(project):
    cells = [{"x": 684, "z": 400, "before": "0000", "after": "0080"}]
    result = project.apply_map_edit(0, header=NEW_BARK["header"], cell=NEW_BARK["cell"],
                                    permissions=cells, label="block a cell")
    assert result["changed"] and result["preview"]["placements_changed"] == 0
    assert result["transaction"]["permissions"][0]["before"] == "0000"
    decoded = decode_member(member_bytes(project.blob, NEW_BARK["member"]))
    changed = project.map_view(header=NEW_BARK["header"], cell=NEW_BARK["cell"])["changed_permission_cells"]
    assert changed == [{"x": 684, "z": 400, "offset": cell_byte(decoded, NEW_BARK["origin"], 684, 400),
                        "value": "0080", "owner": "authored"}]


# ---- placement height is preserved -------------------------------------------


def test_placement_height_cannot_be_edited(project):
    with pytest.raises(EditorError) as error:
        project.apply_map_edit(0, header=NEW_BARK["header"], cell=NEW_BARK["cell"],
                               placement={"slot": NB_SLOT, "x": 684.5, "y": 2})
    assert error.value.code == "UNSUPPORTED_EDIT" and "height is preserved" in str(error.value)
    assert not Project(project.root).doc["map_edits"]


def test_tampered_height_word_refuses_on_reopen(project):
    apply_move(project, NEW_BARK, NB_SLOT, NB_TO, [])
    doc = copy.deepcopy(Project(project.root).doc)
    doc["map_edits"][0]["placements"][0]["record_after"]["y"] += 65536
    atomic_json(project.path, doc)
    with pytest.raises(EditorError) as error:
        Project(project.root)
    assert error.value.code == "UNSUPPORTED_EDIT" and "height is preserved" in str(error.value)


def test_arbitrary_stock_height_is_carried_through_unchanged(project, tmp_path):
    """Slot 7 sits at a fractional height; a move must copy its Y word verbatim."""
    stock = decode_member(member_bytes(project.blob, NEW_BARK["member"]))
    record = stock["records"][7]
    assert record["y"] % 65536 != 0
    view = project.map_view(header=NEW_BARK["header"], cell=NEW_BARK["cell"])
    anchor = next(p for p in view["placements"] if p["slot"] == 7)["position"]
    project.apply_map_edit(0, header=NEW_BARK["header"], cell=NEW_BARK["cell"],
                           placement={"slot": 7, "x": anchor["x"] - 1})
    project.export(tmp_path / "height", 1)
    authored = decode_member(narc_member((tmp_path / "height/game.nds").read_bytes(),
                                         world.MAP_ARCHIVE, NEW_BARK["member"]))
    assert authored["records"][7]["y"] == record["y"]
    assert authored["records"][7]["z"] == record["z"]
    assert authored["records"][7]["x"] == record["x"] - 65536


# ---- forged metadata and malformed input -------------------------------------


def test_forged_record_offset_refuses_on_reopen_and_never_exports(project, tmp_path):
    apply_move(project, NEW_BARK, NB_SLOT, NB_TO, [])
    doc = copy.deepcopy(Project(project.root).doc)
    doc["map_edits"][0]["placements"][0]["record_offset"] += 8
    atomic_json(project.path, doc)
    with pytest.raises(EditorError) as error:
        Project(project.root)
    assert error.value.code == "BEFORE_VALUE_MISMATCH" and "record moved" in str(error.value)
    assert not (tmp_path / "forged").exists()


def test_forged_permission_offset_refuses_on_reopen(project):
    project.apply_map_edit(0, header=NEW_BARK["header"], cell=NEW_BARK["cell"],
                           permissions=[{"x": 684, "z": 400, "after": "0080"}])
    doc = copy.deepcopy(Project(project.root).doc)
    doc["map_edits"][0]["permissions"][0]["offset"] = 4
    atomic_json(project.path, doc)
    with pytest.raises(EditorError) as error:
        Project(project.root)
    assert error.value.code == "BEFORE_VALUE_MISMATCH"


def test_forged_placement_identity_refuses_on_reopen(project):
    apply_move(project, NEW_BARK, NB_SLOT, NB_TO, [])
    clean = copy.deepcopy(Project(project.root).doc)
    for field, value in [("model_id", 999), ("scale_raw", [1, 1, 1]), ("unknown_hex", "ff" * 8)]:
        doc = copy.deepcopy(clean)
        doc["map_edits"][0]["placements"][0][field] = value
        atomic_json(project.path, doc)
        with pytest.raises(EditorError) as error:
            Project(project.root)
        assert error.value.code == "BEFORE_VALUE_MISMATCH"


def test_unknown_transaction_fields_refuse_on_reopen(project):
    apply_move(project, NEW_BARK, NB_SLOT, NB_TO, [])
    doc = copy.deepcopy(Project(project.root).doc)
    doc["map_edits"][0]["extra"] = "surprise"
    atomic_json(project.path, doc)
    with pytest.raises(EditorError) as error:
        Project(project.root)
    assert error.value.code == "UNSUPPORTED_EDIT"


@pytest.mark.parametrize("cell", [
    {"x": 684, "z": 400, "after": "80"}, {"x": 684, "z": 400, "after": "zzzz"},
    {"x": 684.5, "z": 400, "after": "0080"}, {"x": 684, "z": 400},
    {"x": 684, "z": 400, "after": "0080", "offset": 12}, "684,400=0080"])
def test_malformed_permission_requests_refuse(project, cell):
    with pytest.raises(EditorError):
        project.plan_map_edit(header=NEW_BARK["header"], cell=NEW_BARK["cell"], permissions=[cell])


@pytest.mark.parametrize("placement", [
    {"x": 684.5}, {"slot": "13", "x": 684.5}, {"slot": 13, "rotation": 90}, {"slot": 900, "x": 684.5}])
def test_malformed_placement_requests_refuse(project, placement):
    with pytest.raises(EditorError):
        project.plan_map_edit(header=NEW_BARK["header"], cell=NEW_BARK["cell"], placement=placement)


def test_unknown_project_properties_are_preserved(project):
    doc = copy.deepcopy(project.doc)
    doc["future_field"] = {"kept": [1, 2, 3]}
    atomic_json(project.path, doc)
    reopened = Project(project.root)
    apply_move(reopened, NEW_BARK, NB_SLOT, NB_TO, NB_CELLS)
    assert json.loads(project.path.read_text())["future_field"] == {"kept": [1, 2, 3]}
    reopened.undo(1)
    assert json.loads(project.path.read_text())["future_field"] == {"kept": [1, 2, 3]}


# ---- shared map members and origin independence ------------------------------


def test_shared_map_member_views_describe_the_same_resource(project):
    """A member reused by several matrix cells composes as one resource."""
    shared = next(m["map_member"] for m in
                  [{"map_member": c["map_member"]} for c in project.contexts(matrix=0, limit=400)["cells"]]
                  if len(project.contexts(matrix=0, map_member=m["map_member"], limit=2)["cells"]) > 1)
    cells = project.contexts(matrix=0, map_member=shared, limit=2)["cells"]
    first, second = cells[0], cells[1]
    assert first["cell"] != second["cell"] and first["map_member"] == second["map_member"]
    view = project.map_view(header=first["header"], cell=first["cell"])
    ox, oz = view["context"]["origin"]
    tile = (ox + 3, oz + 4)
    stock = view["permissions"]["rows"][4][3]
    after = bytes((bytes.fromhex(stock)[0], bytes.fromhex(stock)[1] ^ 128)).hex()
    project.apply_map_edit(0, header=first["header"], cell=first["cell"],
                           permissions=[{"x": tile[0], "z": tile[1], "before": stock, "after": after}])

    from_first = project.map_view(header=first["header"], cell=first["cell"])
    from_second = project.map_view(header=second["header"], cell=second["cell"])
    assert from_first["context"]["map_member"] == from_second["context"]["map_member"] == shared
    assert from_first["context"]["origin"] != from_second["context"]["origin"]
    # Same resource byte, same value, rendered at each cell's own tile coordinates.
    assert [c["offset"] for c in from_first["changed_permission_cells"]] \
        == [c["offset"] for c in from_second["changed_permission_cells"]]
    assert from_first["changed_permission_cells"][0]["value"] \
        == from_second["changed_permission_cells"][0]["value"] == after
    assert from_second["changed_permission_cells"][0]["x"] == second["origin"][0] + 3
    assert from_second["permissions"]["rows"][4][3] == after
    # The second cell sees the composed value, not the stock byte.
    with pytest.raises(EditorError) as error:
        project.apply_map_edit(1, header=second["header"], cell=second["cell"],
                               permissions=[{"x": second["origin"][0] + 3, "z": second["origin"][1] + 4,
                                             "before": stock, "after": stock}])
    assert error.value.code == "BEFORE_VALUE_MISMATCH"


def test_placement_patches_do_not_depend_on_the_matrix_cell_origin(project):
    """Minimal alias fixture: the same member seen from a shifted cell origin."""
    result = apply_move(project, NEW_BARK, NB_SLOT, NB_TO, NB_CELLS)
    transaction = result["transaction"]
    context = project.context(header=NEW_BARK["header"], cell=NEW_BARK["cell"])
    alias = copy.deepcopy(context)
    alias["origin"] = [context["origin"][0] + 32, context["origin"][1] + 64]
    alias["cell"] = {"x": context["cell"]["x"] + 1, "y": context["cell"]["y"] + 2, "altitude": None}
    assert authoring.patches(alias, transaction) == authoring.patches(context, transaction)
    assert authoring.global_from_record(alias, transaction["placements"][0]["record_after"]) \
        != transaction["placements"][0]["after"]
    assert authoring.transaction_domain(transaction) \
        == {("placement", NEW_BARK["member"], NB_SLOT)} | {
            ("permission", NEW_BARK["member"], c["offset"]) for c in transaction["permissions"]}


# ---- refusals, composition and stale inputs ----------------------------------


def test_wrong_before_value_refuses(project):
    with pytest.raises(EditorError) as error:
        project.apply_map_edit(0, header=NEW_BARK["header"], cell=NEW_BARK["cell"],
                               permissions=[{"x": 684, "z": 400, "before": "0080", "after": "0000"}])
    assert error.value.code == "BEFORE_VALUE_MISMATCH"
    assert not Project(project.root).doc["map_edits"]


def test_unselected_and_unblocked_cells_refuse(project):
    with pytest.raises(EditorError) as error:
        apply_move(project, NEW_BARK, NB_SLOT, NB_TO, [{"x": 684, "z": 400}])
    assert error.value.code == "BEFORE_VALUE_MISMATCH"
    with pytest.raises(EditorError) as error:
        apply_move(project, NEW_BARK, NB_SLOT, {"x": 684.75, "z": 400.5}, NB_CELLS)
    assert error.value.code == "UNQUALIFIED_TARGET"


@pytest.mark.parametrize("target,code", [
    ({"x": 671.5, "z": 400.5}, "OUTSIDE_MAP"),
    ({"x": 705.5, "z": 400.5}, "OUTSIDE_MAP"),
    ({"x": float("inf"), "z": 400.5}, "UNQUALIFIED_TARGET"),
    ({"x": 684.5000001, "z": 400.5}, "UNQUALIFIED_TARGET")])
def test_invalid_targets_never_persist(project, target, code):
    before = project.path.read_bytes()
    with pytest.raises(EditorError) as error:
        apply_move(project, NEW_BARK, NB_SLOT, target, [])
    assert error.value.code == code
    assert project.path.read_bytes() == before


def test_stale_revision_and_reordered_transactions_refuse(project):
    other = Project(project.root)
    apply_move(project, NEW_BARK, NB_SLOT, NB_TO, NB_CELLS)
    with pytest.raises(EditorError) as error:
        apply_move(other, NEW_BARK, NB_SLOT, NB_TO, NB_CELLS, revision=0)
    assert error.value.code == "STALE_REVISION"
    doc = copy.deepcopy(Project(project.root).doc)
    doc["map_edits"][0]["index"] = 3
    atomic_json(project.path, doc)
    with pytest.raises(EditorError) as error:
        Project(project.root)
    assert error.value.code == "STALE_EDIT"


def test_tampered_dependency_digest_refuses(project):
    apply_move(project, NEW_BARK, NB_SLOT, NB_TO, NB_CELLS)
    doc = copy.deepcopy(Project(project.root).doc)
    doc["map_edits"][0]["dependencies"]["baseline_resource"]["sha256"] = "00" * 32
    atomic_json(project.path, doc)
    with pytest.raises(EditorError) as error:
        Project(project.root)
    assert error.value.code == "UNQUALIFIED_DEPENDENCIES"


def test_overlapping_transactions_compose_deterministically(project, tmp_path):
    apply_move(project, CHERRYGROVE, CG_SLOT, CG_TO, CG_CELLS, revision=0)
    second = {"x": 552.5, "y": 1, "z": 398.5}
    moved_cells = [{"x": 551, "z": z} for z in (397, 398, 399)]
    apply_move(project, CHERRYGROVE, CG_SLOT, second, moved_cells, revision=1)
    assert [t["index"] for t in project.doc["map_edits"]] == [0, 1]
    view = project.map_view(header=CHERRYGROVE["header"], cell=CHERRYGROVE["cell"])
    placement = next(p for p in view["placements"] if p["slot"] == CG_SLOT)
    assert placement["position"] == second
    changed = {(c["x"], c["z"]): c["value"] for c in view["changed_permission_cells"]}
    # The middle column was blocked then cleared again: composed to its stock value.
    assert all(changed[(550, z)][2:] == "00" and changed[(551, z)][2:] == "00"
               and changed[(552, z)][2:] == "80" for z in (397, 398, 399))
    report = project.export(tmp_path / "composed", 2)
    stock = member_bytes(project.blob, CHERRYGROVE["member"])
    exported = narc_member((tmp_path / "composed/game.nds").read_bytes(),
                           world.MAP_ARCHIVE, CHERRYGROVE["member"])
    decoded = decode_member(stock)
    for z in (397, 398, 399):
        offset = cell_byte(decoded, CHERRYGROVE["origin"], 551, z)
        assert exported[offset:offset + 2] == stock[offset:offset + 2]
    assert decode_member(exported)["records"][CG_SLOT]["x"] == word(CHERRYGROVE["origin"][0], second["x"])
    assert report["changed_byte_count"] < report["patched_byte_span"]


def test_conflicting_explicit_cells_refuse(project):
    with pytest.raises(EditorError) as error:
        project.plan_map_edit(header=NEW_BARK["header"], cell=NEW_BARK["cell"],
                              placement={"slot": NB_SLOT, "x": NB_TO["x"], "z": NB_TO["z"]},
                              permissions=[{"x": 684, "z": 400, "after": "0000"}],
                              move_collision=NB_CELLS)
    assert error.value.code == "CELL_CONFLICT"


def test_duplicate_cell_before_values_are_kept_and_conflicts_refuse(project):
    # The same cell selected explicitly and moved with the placement: agreeing
    # values merge and keep the guard; disagreeing before-values refuse.
    merged = authoring.merge_cells([{"x": 1, "z": 2, "after": "0080", "before": "0000"}],
                                   [{"x": 1, "z": 2, "after": "0080", "before": "0000"}])
    assert merged == [{"x": 1, "z": 2, "after": "0080", "before": "0000",
                       "source": "selected+moved-with-placement"}]
    with pytest.raises(EditorError) as error:
        authoring.merge_cells([{"x": 1, "z": 2, "after": "0080", "before": "0004"}],
                              [{"x": 1, "z": 2, "after": "0080", "before": "0000"}])
    assert error.value.code == "CELL_CONFLICT"
    with pytest.raises(EditorError) as error:
        project.plan_map_edit(header=NEW_BARK["header"], cell=NEW_BARK["cell"],
                              placement={"slot": NB_SLOT, "x": NB_TO["x"]},
                              permissions=[{"x": 685, "z": 400, "after": "0000", "before": "0000"}],
                              move_collision=NB_CELLS)
    assert error.value.code == "CELL_CONFLICT"


# ---- legacy composition ------------------------------------------------------


def test_legacy_planter_and_authored_edit_compose_when_disjoint(project, tmp_path):
    project.move_placement(5, 14, 564.5, 404.5, 0)
    apply_move(project, NEW_BARK, NB_SLOT, NB_TO, NB_CELLS, revision=1)
    project.move_npc(1, 555, 399, 2)
    changes = {c["operation"] for c in project.diff()}
    assert changes == {"npc.move", "placement.move", "map.transaction"}
    report = project.export(tmp_path / "mixed", 3)
    assert report["changed_byte_count"] > 0
    # Undo peels the operations back one save unit at a time, newest first.
    project.undo(3)
    assert [c["operation"] for c in project.diff()] == ["placement.move", "map.transaction"]
    project.undo(4)
    assert [c["operation"] for c in project.diff()] == ["placement.move"]
    project.undo(5)
    assert project.diff() == []


def test_candidate_pair_sequence_on_the_live_revision_5_state(project, tmp_path):
    """The exact hand-off sequence: live M4 state, then both authored transactions.

    Seeded from the copied live project (revision 5 / history 3) so the accepted
    planter move and trainer edit are present, matching what the supervisor will
    apply to its own copy before exporting one native-acceptance pair.
    """
    live = json.loads(Path("projects/cherrygrove/project.json").read_text())
    atomic_json(project.path, live)
    seeded = Project(project.root)
    assert seeded.doc["revision"] == 5 and len(seeded.doc["history"]) == 3
    assert seeded.doc["placement_moves"]["5:14"]["after"] == {"x": 563.5, "z": 405.5}
    assert seeded.doc["positions"]["1"] == {"x": 555, "z": 399}

    first = seeded.apply_map_edit(5, header=CHERRYGROVE["header"], cell=CHERRYGROVE["cell"],
                                  placement={"slot": CG_SLOT, "x": CG_TO["x"], "z": CG_TO["z"]},
                                  move_collision=CG_CELLS)
    assert first["revision"] == 6 and first["patch_count"] == 7
    second = seeded.apply_map_edit(6, header=NEW_BARK["header"], cell=NEW_BARK["cell"],
                                   placement={"slot": NB_SLOT, "x": NB_TO["x"], "z": NB_TO["z"]},
                                   move_collision=NB_CELLS)
    assert second["revision"] == 7 and second["patch_count"] == 3

    reopened = Project(project.root)
    assert [c["operation"] for c in reopened.diff()] \
        == ["npc.move", "placement.move", "map.transaction", "map.transaction"]
    report = reopened.export(tmp_path / "candidate", 7)
    authored = [p for p in report["patches"] if "map_member" in p]
    assert sum(p["map_member"] == CHERRYGROVE["member"] for p in authored) == 7
    assert sum(p["map_member"] == NEW_BARK["member"] for p in authored) == 3
    assert all(bytes.fromhex(p["before"]) != bytes.fromhex(p["after"])
               or len(p["before"]) > 2 for p in authored)

    # The accepted M4 planter and trainer bytes are untouched by the new work.
    exported = (tmp_path / "candidate/game.nds").read_bytes()
    legacy = [p for p in report["patches"] if "map_member" not in p]
    assert legacy and all(exported[p["rom_offset"]:p["rom_offset"] + len(p["after"]) // 2]
                          == bytes.fromhex(p["after"]) for p in legacy)
    for spec, slot, target in ((CHERRYGROVE, CG_SLOT, CG_TO), (NEW_BARK, NB_SLOT, NB_TO)):
        decoded = decode_member(narc_member(exported, world.MAP_ARCHIVE, spec["member"]))
        assert decoded["records"][slot]["x"] == word(spec["origin"][0], target["x"])
    checklist = (tmp_path / "candidate/PLAYTEST.txt").read_text()
    assert "Authored map transaction 0" in checklist and "Authored map transaction 1" in checklist
    assert "M4 south flower planter" in checklist


def test_overlapping_legacy_and_authored_edits_refuse(project):
    project.move_placement(5, 14, 564.5, 404.5, 0)
    with pytest.raises(EditorError) as error:
        project.apply_map_edit(1, header=CHERRYGROVE["header"], cell=CHERRYGROVE["cell"],
                               placement={"slot": 14, "x": 563.5, "z": 404.5})
    assert error.value.code == "LEGACY_CONFLICT" and "placement 5:14" in str(error.value)
    with pytest.raises(EditorError) as error:
        project.apply_map_edit(1, header=CHERRYGROVE["header"], cell=CHERRYGROVE["cell"],
                               permissions=[{"x": 564, "z": 404, "after": "0000"}])
    assert error.value.code == "LEGACY_CONFLICT" and "permission byte" in str(error.value)
    view = project.map_view(header=CHERRYGROVE["header"], cell=CHERRYGROVE["cell"])
    locked = next(p for p in view["placements"] if p["slot"] == 14)
    assert not locked["editable"] and "planter" in locked["locked_by"]
    assert {c["owner"] for c in view["changed_permission_cells"]} == {"qualified planter operation"}


def test_placement_5_14_is_authorable_when_the_legacy_move_is_absent(project):
    """Not a blacklist: the qualified operation only owns 5:14 while it is active."""
    result = project.apply_map_edit(0, header=CHERRYGROVE["header"], cell=CHERRYGROVE["cell"],
                                    placement={"slot": 14, "x": 566.5, "z": 404.5})
    assert result["changed"]
    assert "not an M3/M4/M5 qualification" in result["transaction"]["qualification"]
    with pytest.raises(EditorError) as error:
        project.move_placement(5, 14, 564.5, 404.5, 1)
    assert error.value.code == "LEGACY_CONFLICT"


def test_authored_collision_is_respected_by_npc_validation(project):
    project.apply_map_edit(0, header=CHERRYGROVE["header"], cell=CHERRYGROVE["cell"],
                           permissions=[{"x": 555, "z": 399, "before": "0004", "after": "0084"}])
    with pytest.raises(EditorError) as error:
        project.move_npc(1, 555, 399, 1)
    assert error.value.code == "BLOCKED_TILE"


# ---- CLI / UI agreement ------------------------------------------------------


def run_cli(*args):
    result = subprocess.run([sys.executable, "-m", "sovereign_editor.cli", *args],
                            capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr


def cli_json(*args):
    code, out, _ = run_cli(*args)
    return code, json.loads(out)


def test_cli_matches_core_and_saves_the_same_transaction(project):
    code, listed = cli_json("map-contexts", "--project", str(project.root), "--matrix", "0",
                            "--map-member", str(NEW_BARK["member"]))
    assert code == 0 and listed["result"] == project.contexts(matrix=0, map_member=NEW_BARK["member"])
    code, searched = cli_json("map-contexts", "--project", str(project.root), "--matrix", "0",
                              "--search", "T20")
    assert code == 0 and [c["cell"] for c in searched["result"]["cells"]] == [NEW_BARK["cell"]]
    code, viewed = cli_json("map-view", "--project", str(project.root), "--header", "60",
                            "--cell", "21,12", "--no-grid")
    assert code == 0 and viewed["result"] == project.map_view(header=60, cell=[21, 12], include_grid=False)
    code, window = cli_json("map-permissions", "--project", str(project.root), "--header", "60",
                            "--cell", "21,12", "--x", "684", "--z", "400", "--width", "2")
    assert code == 0 and [c["value"] for c in window["result"]["cells"]] == ["0000", "0080"]
    code, dry = cli_json("map-edit", "--project", str(project.root), "--header", "60", "--cell", "21,12",
                         "--slot", str(NB_SLOT), "--to-x", "684.5", "--to-z", "400.5",
                         "--move-collision", "685,400", "--dry-run")
    assert code == 0 and dry["result"]["preview"]["permission_cells_changed"] == 2
    assert Project(project.root).doc["revision"] == 0
    code, saved = cli_json("map-edit", "--project", str(project.root), "--header", "60", "--cell", "21,12",
                           "--slot", str(NB_SLOT), "--to-x", "684.5", "--to-z", "400.5",
                           "--move-collision", "685,400", "--revision", "0")
    assert code == 0 and saved["result"]["changed"]
    assert Project(project.root).doc["map_edits"] == [dry["result"]["transaction"]]
    code, refused = cli_json("map-edit", "--project", str(project.root), "--header", "60", "--cell", "21,12",
                             "--slot", str(NB_SLOT), "--to-x", "684.5", "--to-z", "400.5", "--revision", "0")
    assert code == 2 and refused["error"]["code"] == "STALE_REVISION"


def test_cli_permission_argument_forms(project):
    base = ["map-edit", "--project", str(project.root), "--header", "60", "--cell", "21,12", "--dry-run"]
    code, plain = cli_json(*base, "--permission", "684,400=0080")
    assert code == 0 and plain["result"]["transaction"]["permissions"][0]["after"] == "0080"
    code, guarded = cli_json(*base, "--permission", "684,400=0080:0000")
    assert code == 0 and guarded["result"]["transaction"]["permissions"][0]["before"] == "0000"
    code, wrong = cli_json(*base, "--permission", "684,400=0080:0004")
    assert code == 2 and wrong["error"]["code"] == "BEFORE_VALUE_MISMATCH"
    for bad in ("684,400", "684,400=80", "684,400=zzzz", "684,400=0080:80", "684=0080"):
        code, out, err = run_cli(*base, "--permission", bad)
        assert code == 2 and out == ""
        assert "Permission" in err or "X,Z" in err
    # Height is preserved: the hidden flag refuses in the ordinary JSON envelope.
    code, height = cli_json(*base, "--slot", str(NB_SLOT), "--to-y", "2")
    assert code == 2 and height["error"]["code"] == "UNSUPPORTED_EDIT"
    assert "height is preserved" in height["error"]["message"]
    # --revision is required unless the caller asked for a preview.
    code, out, err = run_cli("map-edit", "--project", str(project.root), "--header", "60",
                             "--cell", "21,12", "--permission", "684,400=0080")
    assert code == 2 and "--revision" in err


def test_native_inspector_uses_the_same_operations(project, tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from sovereign_editor.gui import STYLE
    from sovereign_editor.map_inspector import MapInspectorWindow
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window = MapInspectorWindow(project.root, context=(NEW_BARK["header"], NEW_BARK["cell"]))
    window.show()
    app.processEvents()
    assert window.view_data["context"]["map_member"] == NEW_BARK["member"]
    assert window.cell_list.count() > 100 and window.placement_list.count() == 17

    # Select the placement, select its collision cell, move it one tile west.
    window._select_placement(NB_SLOT)
    ox, oz = NEW_BARK["origin"]
    window.toggle_cell(685 - ox, 400 - oz)
    assert window.selected_cells == {(685, 400)}
    window.move_collision.setChecked(True)
    window.x_input.setValue(684.5)
    window.preview()
    app.processEvents()
    assert "cell 685,400" in window.detail.toPlainText()
    assert "0080 -> 0000" in window.detail.toPlainText()
    assert window.detail.toPlainText().startswith("PENDING")
    assert "3 ROM byte patch(es)" in window.detail.toPlainText()
    assert Project(project.root).doc["revision"] == 0
    window.apply_transaction()
    app.processEvents()
    assert window.detail.toPlainText().startswith("SAVED")
    assert "3 ROM byte patch(es)" in window.detail.toPlainText()

    saved = Project(project.root)
    assert saved.doc["revision"] == 1
    transaction = saved.doc["map_edits"][0]
    assert transaction["placements"][0]["after"] == NB_TO
    assert {(c["x"], c["z"]): c["after"] for c in transaction["permissions"]} \
        == {(685, 400): "0000", (684, 400): "0080"}
    # The same request through core produces an identical transaction body.
    reference = Project(project.root)
    reference.undo(1)
    plan = reference.plan_map_edit(header=NEW_BARK["header"], cell=NEW_BARK["cell"],
                                   placement={"slot": NB_SLOT, "x": 684.5, "z": 400.5},
                                   move_collision=NB_CELLS, label=transaction["label"])
    assert plan["transaction"] == transaction

    window.project = Project(project.root)
    window.refresh()
    app.processEvents()
    assert window.placement_list.count() == 17
    window.close()
    app.processEvents()


@pytest.mark.parametrize("start,target", [(1, 12), (12, 1), (0, 1), (65535, 3), (7, 65535)])
def test_anchor_control_keeps_every_16_16_fraction(start, target):
    """A destination the author typed is the destination that is sent."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from sovereign_editor.map_inspector import AnchorSpinBox
    QApplication.instance() or QApplication([])
    box = AnchorSpinBox()
    box.set_anchor(684 + start / 65536)
    assert box.anchor_value() == 684 + start / 65536
    box.setValue(684 + target / 65536)
    assert box.anchor_value() == 684 + target / 65536
    assert round((box.anchor_value() - 684) * 65536) == target
    # Whole-tile stepping from a fraction stays on the same fraction.
    box.set_anchor(684 + start / 65536)
    box.setValue(box.value() + 1)
    assert box.anchor_value() == 685 + start / 65536


def test_inspector_preserves_exact_fractional_anchors(project):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from sovereign_editor.map_inspector import MapInspectorWindow
    app = QApplication.instance() or QApplication([])
    window = MapInspectorWindow(project.root, context=(NEW_BARK["header"], NEW_BARK["cell"]))
    app.processEvents()
    stock = decode_member(member_bytes(project.blob, NEW_BARK["member"]))["records"][NB_FRACTIONAL_SLOT]
    assert stock["x"] % 65536 and stock["z"] % 65536, "this slot must have real fractions"
    exact = next(p for p in window.view_data["placements"] if p["slot"] == NB_FRACTIONAL_SLOT)["position"]
    window._select_placement(NB_FRACTIONAL_SLOT)
    app.processEvents()
    # Untouched controls return the exact record anchors, so applying is a no-op.
    assert window._request()["placement"] == {"slot": NB_FRACTIONAL_SLOT, "x": exact["x"], "z": exact["z"]}
    assert window.project.plan_map_edit(**window._request())["empty"]
    # Moving one axis keeps the other axis' fraction bit-exact.
    window.x_input.setValue(window.x_input.value() - 1)
    request = window._request()["placement"]
    assert request["z"] == exact["z"] and request["x"] == exact["x"] - 1
    plan = window.project.plan_map_edit(**window._request())
    change = plan["transaction"]["placements"][0]
    assert change["record_after"] == {"x": stock["x"] - 65536, "y": stock["y"], "z": stock["z"]}
    assert [p["kind"] for p in plan["patches"]] == ["placement.x"]
    # A sub-tile destination lands on exactly the requested record word.
    window.x_input.setValue(exact["x"] + 11 / 65536)
    change = window.project.plan_map_edit(**window._request())["transaction"]["placements"][0]
    assert change["record_after"] == {"x": stock["x"] + 11, "y": stock["y"], "z": stock["z"]}
    # A value between record steps is refused by core, never quietly snapped.
    window.x_input.setValue(exact["x"] + 0.5 / 65536)
    with pytest.raises(EditorError) as error:
        window.project.plan_map_edit(**window._request())
    assert error.value.code == "UNQUALIFIED_TARGET"
    window.close()
    app.processEvents()


def test_inspector_screenshot_shows_an_applied_transaction(project, tmp_path):
    """Visual QA only: styled window, selected cells and a saved transaction.

    A screenshot is not native melonDS acceptance and never claims runtime safety.
    The capture goes to pytest scratch: a test must never write over the protected
    historical evidence of an earlier milestone.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from sovereign_editor.gui import STYLE
    from sovereign_editor.map_inspector import MapInspectorWindow
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window = MapInspectorWindow(project.root, context=(NEW_BARK["header"], NEW_BARK["cell"]))
    window.resize(1360, 880)
    window.show()
    app.processEvents()
    ox, oz = NEW_BARK["origin"]
    window._select_placement(NB_SLOT)
    window.toggle_cell(685 - ox, 400 - oz)
    window.move_collision.setChecked(True)
    window.x_input.setValue(684.5)
    window.apply_transaction()
    app.processEvents()
    # Keep a live selection visible next to the applied change for the capture.
    window.toggle_cell(683 - ox, 400 - oz)
    window.fit_map()
    app.processEvents()
    assert window.detail.toPlainText().startswith("SAVED")
    assert len(window.view_data["changed_permission_cells"]) == 2
    evidence = tmp_path / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    target = evidence / "new-bark-inspector.png"
    assert window.grab().save(str(target)) and target.stat().st_size > 20000
    window.close()
    app.processEvents()


def test_inspector_exports_a_rom_without_overwriting(project, tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from sovereign_editor.map_inspector import MapInspectorWindow
    app = QApplication.instance() or QApplication([])
    window = MapInspectorWindow(project.root, context=(NEW_BARK["header"], NEW_BARK["cell"]))
    app.processEvents()
    window._select_placement(NB_SLOT)
    window.toggle_cell(685 - NEW_BARK["origin"][0], 400 - NEW_BARK["origin"][1])
    window.move_collision.setChecked(True)
    window.x_input.setValue(684.5)
    window.apply_transaction()
    app.processEvents()
    save = tmp_path / "paired.sav"
    save.write_bytes(b"\x11" * 524288)
    report = window.export_to(tmp_path / "ui-export", save)
    assert report and report["changed_byte_count"] == 3
    assert (tmp_path / "ui-export/game.nds").is_file()
    assert (tmp_path / "ui-export/game.sav").read_bytes() == save.read_bytes()
    assert "authored map transaction" in (tmp_path / "ui-export/PLAYTEST.txt").read_text().lower()
    assert window.export_to(tmp_path / "ui-export") is None
    assert "existing exports are preserved" in window.message.text()
    exported = decode_member(narc_member((tmp_path / "ui-export/game.nds").read_bytes(),
                                         world.MAP_ARCHIVE, NEW_BARK["member"]))
    assert exported["records"][NB_SLOT]["x"] == word(NEW_BARK["origin"][0], NB_TO["x"])
    window.close()
    app.processEvents()
