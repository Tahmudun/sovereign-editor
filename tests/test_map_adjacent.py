"""Adjacent-cell editing and the identified New Bark background interaction.

ROM readback uses ndspy's archive extraction and independent struct offsets.
These checks establish software behavior, not native melonDS acceptance.
"""
import copy
import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import ndspy.narc
import ndspy.rom
import pytest

from sovereign_editor.core import Project, atomic_json
from sovereign_editor.formats import EditorError

NB = {"header": 60, "cell": [21, 12]}
CG = {"header": 67, "cell": [17, 12]}
ROUTE = {"header": 33, "cell": [18, 12]}


@pytest.fixture(scope="module")
def workspace(tmp_path_factory):
    source = Path(os.environ.get("SG_TEST_ROM", "projects/cherrygrove/baseline.nds"))
    if not source.exists():
        pytest.skip("A qualified HeartGold baseline is required")
    p = Project.create(source, tmp_path_factory.mktemp("map-adjacent") / "project")
    return p.root, copy.deepcopy(p.doc)


@pytest.fixture
def project(workspace):
    root, original = workspace
    atomic_json(root / "project.json", original)
    yield Project(root)
    atomic_json(root / "project.json", original)


def request():
    return dict(**NB, placement={"slot": 13, "x": 684.5, "z": 400.5},
                move_collision=[{"x": 685, "z": 400}], align_sign=True)


def archive(rom, path, member):
    return ndspy.narc.NARC(rom.files[rom.filenames.idOf(path)]).files[member]


def test_sign_move_export_preserves_every_other_byte_and_undo(project, tmp_path):
    manifest = project.path.read_bytes()
    # Already aligned in baseline: exact no-op, including no manifest rewrite.
    assert project.apply_map_edit(0, **NB, align_sign=True)["changed"] is False
    assert project.path.read_bytes() == manifest
    baseline = project.export(tmp_path / "no-op", 0)
    assert baseline["changed_byte_count"] == 0
    assert (tmp_path / "no-op/game.nds").read_bytes() == project.blob
    plan = project.plan_map_edit(**request())
    assert project.path.read_bytes() == manifest
    assert plan["transaction"]["sign_interaction"]["to"] == [684, 400]
    result = project.apply_map_edit(0, **request())
    assert result["revision"] == 1 and len(project.doc["history"]) == 1
    p = Project(project.root)
    assert p.map_sign(**NB)["position"] == [684, 400]
    with pytest.raises(EditorError, match="not blocked"):
        p.plan_map_edit(**request())  # The vacated source is no longer a selectable blocker.
    # A request against the resulting state is a no-op with no rewrite.
    saved = p.path.read_bytes()
    assert p.apply_map_edit(1, **NB, align_sign=True)["changed"] is False
    assert p.path.read_bytes() == saved
    exported = p.export(tmp_path / "moved", 1)
    raw_rom = (tmp_path / "moved/game.nds").read_bytes()
    rom, original = ndspy.rom.NintendoDSRom(raw_rom), ndspy.rom.NintendoDSRom(project.blob)
    event = archive(rom, "a/0/3/2", 57)
    old_event = archive(original, "a/0/3/2", 57)
    assert struct.unpack_from("<HH4i", event, 4 + 2 * 20) == (15, 1, 684, 400, 0, 4)
    restored_event = bytearray(event)
    restored_event[48:52] = old_event[48:52]
    assert restored_event == old_event
    for path, member in [("a/0/1/2", 842), ("a/0/1/2", 615), ("a/0/2/7", 542)]:
        assert archive(rom, path, member) == archive(original, path, member)
    raw_map = archive(rom, "a/0/6/5", 0)
    bgs = struct.unpack_from("<H", raw_map, 18)[0]
    permissions = 20 + bgs
    buildings = permissions + 2048
    assert struct.unpack_from("<3i", raw_map, buildings + 13 * 48 + 4) == (-229376, 65536, 32768)
    assert raw_map[permissions + 2 * (16 * 32 + 12):permissions + 2 * (16 * 32 + 12) + 2] == b"\x00\x80"
    assert raw_map[permissions + 2 * (16 * 32 + 13):permissions + 2 * (16 * 32 + 13) + 2] == b"\x00\x00"
    restored = bytearray(raw_rom)
    for patch in reversed(exported["patches"]):
        offset = patch["rom_offset"]
        restored[offset:offset + len(patch["before"]) // 2] = bytes.fromhex(patch["before"])
    assert restored == project.blob
    p.undo(1)
    assert p.map_sign(**NB)["position"] == [685, 400]
    assert p.export(tmp_path / "undo", 2)["changed_byte_count"] == 0


def test_repair_existing_r7_and_mixed_undo(project):
    retained = json.loads(Path("projects/map-authoring-1/project.json").read_text())
    assert retained["revision"] == 7
    retained["custom_unknown_property"] = {"keep": True}
    atomic_json(project.path, retained)
    p = Project(project.root)
    before = p.path.read_bytes()
    plan = p.plan_map_edit(**NB, align_sign=True)
    assert len(plan["patches"]) == 1 and plan["patches"][0]["kind"] == "sign-interaction.x"
    assert not plan["transaction"]["placements"] and not plan["transaction"]["permissions"]
    assert p.path.read_bytes() == before
    p.apply_map_edit(7, **NB, align_sign=True)
    assert p.doc["revision"] == 8 and p.doc["custom_unknown_property"] == {"keep": True}
    with pytest.raises(EditorError, match="Project changed"):
        p.apply_map_edit(7, **NB, align_sign=True)
    p.undo(8)
    assert p.doc["map_edits"] == retained["map_edits"]
    p.undo(9)
    assert len(p.doc["map_edits"]) == 1
    assert p.map_sign(**NB)["position"] == [685, 400]


@pytest.mark.parametrize("tamper", ["before", "after", "offset", "dependency", "event", "target"])
def test_forged_sign_edits_refuse_on_reopen(project, tamper):
    project.apply_map_edit(0, **request())
    doc = copy.deepcopy(project.doc)
    change = doc["map_edits"][0]["sign_interaction"]
    if tamper == "before": change["before"] = "00" * 20
    elif tamper == "after": change["after"] = "00" * 20
    elif tamper == "offset": change["record_offset"] += 20
    elif tamper == "dependency": change["dependencies"][1]["sha256"] = "0" * 64
    elif tamper == "event": change["event_id"] = 1
    else: change["to"] = [1, 2]
    atomic_json(project.path, doc)
    with pytest.raises(EditorError, match="Sign interaction"):
        Project(project.root)


def test_interaction_rejects_unidentified_sign_and_fractional_target(project):
    with pytest.raises(EditorError, match="Only the identified"):
        project.plan_map_edit(**CG, align_sign=True)
    with pytest.raises(EditorError, match="Select the identified"):
        project.plan_map_edit(**NB, placement={"slot": 14}, align_sign=True)
    with pytest.raises(EditorError, match="tile-centered"):
        project.plan_map_edit(**NB, placement={"slot": 13, "x": 684.25}, align_sign=True)
    with pytest.raises(EditorError, match="Another background"):
        project.plan_map_edit(**NB, placement={"slot": 13, "x": 682.5, "z": 393.5}, align_sign=True)
    assert project.doc["revision"] == 0


def test_repeated_aligned_moves_compose_and_undo(project, tmp_path):
    project.apply_map_edit(0, **request())
    project.apply_map_edit(1, **NB, placement={"slot": 13, "x": 683.5},
                           move_collision=[{"x": 684, "z": 400}], align_sign=True)
    assert Project(project.root).map_sign(**NB)["position"] == [683, 400]
    project.export(tmp_path / "twice", 2)
    project.undo(2)
    assert project.map_sign(**NB)["position"] == [684, 400]


def test_neighborhood_resolves_each_header_and_reused_member(project):
    result = project.map_neighborhood(**CG, image=False)
    by_cell = {tuple(e["cell"]): e for e in result["cells"]}
    assert len(result["cells"]) == 9
    assert by_cell[17, 12]["active"] and by_cell[17, 12]["map_member"] == 5
    route = by_cell[18, 12]
    assert route["header"] == 33 and route["map_member"] == 1
    assert route["context"]["origin"] == [576, 384]
    assert route["scene"]["resources"]["terrain"]["name"] == "map18_12c"
    # Adjacent filler cells reuse the same map member. Their resource data is shared.
    assert by_cell[18, 11]["map_member"] == by_cell[18, 13]["map_member"] == 208
    p = project.permission_cells(header=0, cell=[18, 11], x=577, z=353)["cells"][0]
    after = (bytes.fromhex(p["value"])[0:1] + bytes([bytes.fromhex(p["value"])[1] ^ 128])).hex()
    project.apply_map_edit(0, header=0, cell=[18, 11], permissions=[{"x": 577, "z": 353, "after": after}])
    assert project.permission_cells(header=0, cell=[18, 13], x=577, z=417)["cells"][0]["value"] == after


def test_headerless_neighborhood_and_render_failure_isolation(project, monkeypatch):
    lab = project.map_neighborhood(header=61, cell=[0, 0], image=False)
    assert len(lab["cells"]) == 1 and lab["cells"][0]["header"] == 61
    original = project.map_scene
    def fail_one(**kwargs):
        if kwargs.get("cell") == [18, 12]:
            raise EditorError("UNSUPPORTED_SCENE", "test rendering variant")
        return original(**kwargs)
    monkeypatch.setattr(project, "map_scene", fail_one)
    result = project.map_neighborhood(**CG, image=False)
    route = next(c for c in result["cells"] if c["cell"] == [18, 12])
    assert route["status"] == "ok" and route["scene_error"] == "test rendering variant"
    assert any(c.get("scene") for c in result["cells"] if c["cell"] != [18, 12])


def test_edits_on_both_sides_keep_resource_offsets_and_undo(project, tmp_path):
    for revision, (context, x) in enumerate([(CG, 575), (ROUTE, 576)]):
        current = project.permission_cells(**context, x=x, z=398)["cells"][0]["value"]
        pair = bytes.fromhex(current)
        after = bytes([pair[0], pair[1] ^ 128]).hex()
        project.apply_map_edit(revision, **context, permissions=[{"x": x, "z": 398, "before": current, "after": after}])
    changes = project.doc["map_edits"]
    assert [t["context"]["map_member"] for t in changes] == [5, 1]
    result = project.export(tmp_path / "adjacent", 2)
    assert result["changed_byte_count"] == 2
    assert len({p["map_member"] for p in result["patches"]}) == 2
    with pytest.raises(EditorError, match="outside map cell"):
        project.plan_map_edit(**CG, permissions=[{"x": 576, "z": 398, "after": "0000"}])
    project.undo(2)
    assert project.doc["map_edits"] == changes[:1]


def open_window(project, context=CG):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from sovereign_editor.map_inspector import MapInspectorWindow
    app = QApplication.instance() or QApplication([])
    window = MapInspectorWindow(project, context=(context["header"], context["cell"]))
    window.show()
    app.processEvents()
    return app, window


def test_mouse_neighbor_activation_preserves_staging_and_owns_edits(project):
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtTest import QTest
    app, w = open_window(project)
    w.neighbors_toggle.setChecked(True)
    app.processEvents()
    # One real viewport click in the neighboring Route 29 cell activates its header.
    point = w.grid.mapFromScene(QPointF(48 * 32, 16 * 32))
    QTest.mouseClick(w.grid.viewport(), Qt.MouseButton.LeftButton, pos=point)
    app.processEvents()
    assert w.header == 33 and w.cell == [18, 12]
    w.set_tool("block")
    w.paint_tile(1, 14)
    if not w.staged_cells:
        w.set_tool("unblock")
        w.paint_tile(1, 14)
    staged = copy.deepcopy(w.staged_cells)
    w.paint_tile(-1, 14)
    assert w.staged_cells == staged
    w.set_tool("select")
    assert w.activate_neighbor(-16, 16)
    assert w.cell == [18, 12] and w.staged_cells == staged
    w.apply_transaction()
    assert project.doc["map_edits"][-1]["context"]["map_member"] == 1
    w.activate_neighbor(-16, 16)
    assert w.cell == [17, 12] and not w.staged_cells
    w.close()
    app.processEvents()


def test_ui_sign_option_uses_shared_transaction_and_cancel(project):
    app, w = open_window(project, NB)
    w.select_placement(13)
    assert w.align_sign.isVisible()
    w.drag_placement(13, -1, 0)
    w.align_sign.setChecked(True)
    w.move_collision.setChecked(True)
    w.toggle_cell(13, 16)
    w.preview()
    assert "Town-sign text interaction: [685, 400] -> [684, 400]" in w.detail.toPlainText()
    w.apply_transaction()
    assert Project(project.root).map_sign(**NB)["position"] == [684, 400]
    assert len(project.doc["history"]) == 1
    w.undo()
    assert Project(project.root).map_sign(**NB)["position"] == [685, 400]
    w.select_placement(13)
    w.align_sign.setChecked(True)
    w.cancel_staged()
    assert not w.align_sign.isChecked()
    w.select_placement(14)
    assert not w.align_sign.isVisible()
    w.close()
    app.processEvents()


def test_cli_align_sign_matches_core(project):
    args = [sys.executable, "-m", "sovereign_editor.cli", "map-edit", "--project", str(project.root),
            "--header", "60", "--cell", "21,12", "--slot", "13", "--to-x", "684.5", "--to-z", "400.5",
            "--move-collision", "685,400", "--align-sign", "--dry-run"]
    completed = subprocess.run(args, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout)["result"] == project.plan_map_edit(**request())
