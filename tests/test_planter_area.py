"""M4: finite qualification, independent exports and UI/CLI transaction agreement."""
import copy
import json
import os
from pathlib import Path
import struct
import subprocess
import sys

import ndspy.narc
import pytest

from sovereign_editor.core import Project, atomic_json
from sovereign_editor.decoration import (BEFORE, TARGETS, authored_move, qualify_move, qualify_area,
                                         translation_proof, footprint)
from sovereign_editor.formats import EditorError, file_span
from test_editor import project, workspace
from historical import exported


def independent_rom(baseline, x, z, trainer=True):
    result = bytearray(baseline)
    if trainer:
        result[57611220] = 43
    struct.pack_into("<i", result, 66281892, int((x - 560) * 65536))
    struct.pack_into("<i", result, 66281900, int((z - 400) * 65536))
    for zz in (403, 404, 405):
        result[66279168 + 2 * ((zz - 384) * 32 + 565 - 544) + 1] = 0
    for zz in (int(z) - 1, int(z), int(z) + 1):
        result[66279168 + 2 * ((zz - 384) * 32 + int(x) - 544) + 1] = 128
    return result


def test_every_anchor_export_reopen_overlap_and_baseline_restore(project, tmp_path):
    project.move_npc(1, 555, 399, 0)
    baseline = project.blob
    other_props = [p for p in project.scene()["props"] if p["slot"] != 14]
    previous = None
    for i, (x, z) in enumerate(((564.5, 404.5), (564.5, 405.5), (563.5, 405.5), (563.5, 404.5), (565.5, 404.5))):
        result = project.move_placement(5, 14, x, z, project.doc["revision"])
        assert result["changed"]
        project = Project(project.root)
        assert project.placement(5, 14)["global_position"] == [x, 1, z]
        assert other_props == [p for p in project.scene()["props"] if p["slot"] != 14]
        assert project.maps[5][1][14]["xyz"] == [5.5, 1, 4.5]
        for xx in (563, 564, 565):
            for zz in range(403, 407):
                assert project.tile(xx, zz)["blocked"] == (xx == int(x) and int(z) - 1 <= zz <= int(z) + 1)
        output = tmp_path / f"anchor-{i}"
        report = project.export(output, project.doc["revision"])
        data = (output / "game.nds").read_bytes()
        assert data == independent_rom(baseline, x, z)
        assert report["changed_byte_count"] == (1 if x == 565.5 else 8 if z == 404.5 else 9)
        archive = ndspy.narc.NARC(file_span(data, "a/0/6/5")[1])
        assert struct.unpack_from("<3i", archive.files[5], 2760) == (int((x-560)*65536), 65536, int((z-400)*65536))
        if i == 1:
            # Same-column Z translation retains both overlapping blockers exactly.
            assert sum(a != b for a, b in zip(previous, data)) == 3
        previous = data
        before = project.path.read_bytes()
        assert not project.move_placement(5, 14, x, z, project.doc["revision"])["changed"]
        assert project.path.read_bytes() == before
    assert (project.root / "baseline.nds").read_bytes() == baseline


def test_legacy_m3_read_noop_export_and_mixed_undo(project, tmp_path):
    legacy = json.loads(Path("evidence/m4/before/projects/cherrygrove/project.json").read_text())
    legacy["user_annotation"] = {"keep": ["unknown", 17]}
    atomic_json(project.path, legacy)
    original = project.path.read_bytes()
    project = Project(project.root)
    assert project.path.read_bytes() == original
    assert not project.move_placement(5, 14, 564.5, 404.5, 4)["changed"]
    assert project.path.read_bytes() == original
    project.export(tmp_path / "legacy", 4)
    project.move_placement(5, 14, 563.5, 405.5, 4)
    assert project.doc["history"][-1]["placement_moves"] == legacy["placement_moves"]
    project.move_npc(1, 554, 399, 5)
    project.move_placement(5, 14, 564.5, 405.5, 6)
    for revision in (7, 8, 9):
        Project(project.root).undo(revision)
    project = Project(project.root)
    assert project.doc["positions"] == legacy["positions"]
    assert project.doc["placement_moves"] == legacy["placement_moves"]
    assert project.doc["history"] == legacy["history"]
    assert project.doc["user_annotation"] == legacy["user_annotation"]
    project.undo(10)
    project.undo(11)
    project.export(tmp_path / "noop", 12)
    assert (tmp_path / "noop/game.nds").read_bytes() == project.blob
    assert (tmp_path / "legacy/game.nds").read_bytes() == exported("projects/cherrygrove/exports/planter-west-r4/game.nds")


@pytest.mark.parametrize("x,z", TARGETS[1:])
def test_each_destination_conflicts_in_both_operation_orders(project, x, z):
    project.move_npc(1, int(x), int(z), 0)
    before = project.path.read_bytes()
    with pytest.raises(EditorError) as error:
        project.move_placement(5, 14, x, z, 1)
    assert error.value.code == "BLOCKED_TILE" and project.path.read_bytes() == before
    project.undo(1)
    project.move_placement(5, 14, x, z, 2)
    before = project.path.read_bytes()
    with pytest.raises(EditorError) as error:
        project.move_npc(1, int(x), int(z), 3)
    assert error.value.code == "BLOCKED_TILE" and project.path.read_bytes() == before


@pytest.mark.parametrize("x,z", [(562.5,404.5), (565.5,405.5), (564.5,403.5), (563.5,406.5),
                                 (563,404.5), (563.6,404.5), (563.5,405), (563.5,float("nan"))])
def test_unqualified_neighbors_and_fractional_moves_refuse(project, x, z):
    before = project.path.read_bytes()
    with pytest.raises(EditorError) as error:
        project.move_placement(5, 14, x, z, 0)
    assert error.value.code == "UNQUALIFIED_TARGET" and project.path.read_bytes() == before


@pytest.mark.parametrize("section,record,code", [
    ("warps", {"x": 563, "z": 407}, "DOOR_CONFLICT"),
    ("triggers", {"x": 563, "z": 406, "width": 1, "height": 1}, "TRIGGER_CONFLICT"),
    ("backgrounds", {"x": 562, "z": 406}, "BACKGROUND_CONFLICT"),
    ("npcs", {"x": 563, "z": 407, "range_x": 0, "range_z": 0}, "OCCUPIED_TILE"),
])
def test_new_area_interaction_dependencies_checked(project, section, record, code):
    legacy = qualify_move(project)
    project.base_events[section][0].update(record)
    with pytest.raises(EditorError) as error:
        qualify_area(project, legacy)
    assert error.value.code == code


def test_all_eleven_cell_before_values_and_height_are_required(project, monkeypatch):
    from sovereign_editor import decoration
    legacy = qualify_move(project)
    proof = qualify_area(project, legacy)
    assert len(proof["area_cells"]) == 11
    assert proof["dependencies"]["local_bypass"]["all_targets_connected"]
    original = project.blob
    for cell in proof["area_cells"]:
        changed = bytearray(original)
        changed[cell["rom_offset"] + 1] ^= 128
        project.blob = changed
        with pytest.raises(EditorError) as error:
            qualify_area(project, legacy)
        assert error.value.code == "BEFORE_VALUE_MISMATCH"
    project.blob = original
    plates = decoration.flat_height_plates(__import__('sovereign_editor').formats.resource(original, 'a/0/6/5', 5)[1])
    # A second plane over a newly allowed edge cell must not be hidden by plate 6.
    plates.append({"index": 99, "bounds": [3, 6, 4, 7], "height": .5})
    monkeypatch.setattr(decoration, "flat_height_plates", lambda _: plates)
    with pytest.raises(EditorError) as error:
        qualify_area(project, legacy)
    assert error.value.code == "UNQUALIFIED_HEIGHT"


@pytest.mark.parametrize("field", ["record_before", "dependencies_sha256", "before", "after", "id"])
def test_new_authored_guards_and_history_tampering(project, field):
    project.move_placement(5, 14, 563.5, 405.5, 0)
    valid = copy.deepcopy(project.doc)
    invalid = copy.deepcopy(valid)
    invalid["placement_moves"]["5:14"][field] = "tampered"
    atomic_json(project.path, invalid)
    before = project.path.read_bytes()
    with pytest.raises(EditorError) as error:
        Project(project.root)
    assert error.value.code == "BEFORE_VALUE_MISMATCH" and project.path.read_bytes() == before
    valid["history"].append({"operation": "placement.move", "positions": {}, "placement_moves": invalid["placement_moves"]})
    atomic_json(project.path, valid)
    before = project.path.read_bytes()
    with pytest.raises(EditorError):
        Project(project.root).undo(1)
    assert project.path.read_bytes() == before


def drag_planter(window, dx, dz):
    from PySide6.QtCore import Qt, QPointF
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from sovereign_editor.gui import TILE
    item = window.placement_items[(5, 14)]
    start = window.view.mapFromScene(item.mapToScene(item.rect().center()))
    finish = window.view.mapFromScene(item.mapToScene(item.rect().center()) + QPointF(dx*TILE, dz*TILE))
    QTest.mousePress(window.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    QTest.mouseMove(window.view.viewport(), finish, 50)
    QTest.mouseRelease(window.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, finish)
    QApplication.processEvents()


def test_native_drag_inspector_cli_agreement_refusal_and_stale(project):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QGraphicsItem
    from sovereign_editor.gui import EditorWindow
    app = QApplication.instance() or QApplication([])
    window = EditorWindow(project.root)
    window.show()
    app.processEvents()
    window.focus_east()
    window.select_placement(5, 14)
    before_image = window.project.preview()["image"]
    drag_planter(window, -1.85, 1.1)
    assert Project(project.root).placement(5, 14)["global_position"] == [563.5, 1, 405.5]
    assert window.placement_x_input.value() == 563.5 and window.placement_z_input.value() == 405.5
    assert before_image != window.project.preview()["image"]
    before = project.path.read_bytes()
    drag_planter(window, 2, 0)  # Whitelist hole at 565.5,405.5.
    assert "Unqualified target" in window.message.text()
    assert project.path.read_bytes() == before
    assert window.placement_items[(5, 14)].pos().isNull()
    window.placement_x_input.setValue(564.5)
    QTest.mouseClick(window.placement_move_button, Qt.MouseButton.LeftButton)
    app.processEvents()
    prefix = [sys.executable, "-m", "sovereign_editor.cli"]
    query = subprocess.run(prefix + ["placement", "--project", str(project.root), "--map", "5", "--slot", "14"], capture_output=True, text=True)
    assert query.returncode == 0
    assert json.loads(query.stdout)["result"] == next(p for p in window.placement_data if p["key"] == "5:14")
    command = prefix + ["move-placement", "--project", str(project.root), "--map", "5", "--slot", "14", "--x", "563.5", "--z", "404.5", "--revision", "2"]
    moved = subprocess.run(command, capture_output=True, text=True)
    assert moved.returncode == 0
    before = project.path.read_bytes()
    drag_planter(window, -1, 0)  # Window holds revision 2; CLI saved revision 3.
    assert "Project changed" in window.message.text() and project.path.read_bytes() == before
    assert window.placement_x_input.value() == 563.5 and window.placement_z_input.value() == 404.5
    window.reload()
    assert window.selected_placement == (5, 14)
    QTest.mouseClick(window.undo_button, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert window.placement_x_input.value() == 564.5 and window.placement_z_input.value() == 405.5
    for p in window.placement_data:
        window.select_placement(p["map"], p["slot"])
        assert bool(window.placement_items[(p["map"], p["slot"])].flags() & QGraphicsItem.GraphicsItemFlag.ItemIsMovable) == p["editable"]
        assert window.placement_fields.isVisible() == p["editable"]
    window.close()
    app.processEvents()
