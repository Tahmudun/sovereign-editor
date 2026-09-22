"""M3 guarded placement + collision composition, with independent byte readback."""
import copy
import json
import os
import struct
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import ndspy.narc
import pytest

from sovereign_editor.core import Project, atomic_json
from sovereign_editor.decoration import qualify_move
from sovereign_editor.formats import EditorError, file_span, resource, flat_height_plates
from test_editor import project, workspace


def test_native_qualification_is_independent_of_preview_bounds(project):
    proof = qualify_move(project)
    assert proof["collision_updates_required"]
    assert len(proof["collision_cells"]) == 6
    assert proof["height_plate"] == {"index": 6, "bounds": [0, 0, 8, 8], "height": 1}
    assert proof["direct_event_conflicts"] == []
    assert [b["script"] for b in project.base_events["backgrounds"]] == [6, 7, 8001, 8225]
    # A newly introduced explicit dependency must refuse even with the same mesh.
    project.base_events["warps"][0].update(x=564, z=404)
    with pytest.raises(EditorError) as error:
        qualify_move(project)
    assert error.value.code == "DOOR_CONFLICT"


@pytest.mark.parametrize("section,record,code", [
    ("triggers", {"x": 564, "z": 402, "width": 1, "height": 4}, "TRIGGER_CONFLICT"),
    ("backgrounds", {"x": 563, "z": 404}, "BACKGROUND_CONFLICT"),
    ("npcs", {"x": 564, "z": 404}, "OCCUPIED_TILE"),
])
def test_complete_event_dependency_checks(project, section, record, code):
    project.base_events[section][0].update(record)
    with pytest.raises(EditorError) as error:
        qualify_move(project)
    assert error.value.code == code


def test_placement_collision_export_reopen_mixed_undo(project, tmp_path):
    baseline = project.blob
    project.move_npc(1, 555, 399, 0)
    # Simulate the real v0.1 history without the newer placement state field.
    doc = copy.deepcopy(project.doc)
    doc.pop("placement_moves")
    doc["history"][0].pop("placement_moves")
    atomic_json(project.path, doc)
    legacy = project.path.read_bytes()
    project = Project(project.root)
    assert project.path.read_bytes() == legacy
    result = project.move_placement(5, 14, 564.5, 404.5, 1)
    assert result["revision"] == 2
    reopened = Project(project.root)
    assert reopened.placement(5, 14)["global_position"] == [564.5, 1, 404.5]
    assert reopened.maps[5][1][14]["xyz"] == [5.5, 1, 4.5]  # Immutable decoded source.
    for z in (403, 404, 405):
        assert not reopened.tile(565, z)["blocked"] and reopened.tile(564, z)["blocked"]
    save = tmp_path / "input.sav"
    save.write_bytes(b"\x37" * 524288)
    report = reopened.export(tmp_path / "changed", 2, save)
    data = (tmp_path / "changed/game.nds").read_bytes()
    assert (tmp_path / "changed/game.sav").read_bytes() == save.read_bytes()
    assert report["changed_byte_count"] == 8 and report["native_acceptance"] == "pending"
    # Construct expectations directly from documented absolute offsets, independently
    # of exporter reports/helpers. Exact equality proves every unknown byte survives.
    expected = bytearray(baseline)
    expected[57611220] = 43
    expected[66281894] = 4  # 16.16 X: 5.5 -> 4.5, one changed byte.
    for pos in (66280427, 66280491, 66280555):
        expected[pos] = 0
    for pos in (66280425, 66280489, 66280553):
        expected[pos] = 128
    assert data == expected
    _, original_archive = file_span(baseline, "a/0/6/5")
    _, changed_archive = file_span(data, "a/0/6/5")
    a, b = ndspy.narc.NARC(original_archive), ndspy.narc.NARC(changed_archive)
    assert [i for i, (x, y) in enumerate(zip(a.files, b.files)) if x != y] == [5]
    # Parse record offset directly with an independent NARC implementation.
    assert struct.unpack_from("<3i", b.files[5], 20 + 16 + 2048 + 14 * 48 + 4) == (294912, 65536, 294912)
    assert project.blob == (project.root / "baseline.nds").read_bytes() == baseline
    reopened.undo(2)
    assert reopened.doc["positions"] == {"1": {"x": 555, "z": 399}} and not reopened.doc["placement_moves"]
    npc_only = reopened.export(tmp_path / "npc-only", 3)
    assert npc_only["changed_byte_count"] == 1
    reopened.undo(3)
    noop = reopened.export(tmp_path / "noop", 4)
    assert noop["changed_byte_count"] == 0
    assert (tmp_path / "noop/game.nds").read_bytes() == baseline


@pytest.mark.parametrize("map_id,slot,x,z", [
    (5, 12, 551.5, 398.5), (5, 4, 563.5, 390), (4, 14, 564.5, 404.5),
    (5, 14, 563.5, 403.5), (5, 14, 565.5, 405.5), (5, 14, True, 404.5),
    (5, 14, float("nan"), 404.5), (5, 14, float("inf"), 404.5), (True, 14, 564.5, 404.5),
])
def test_unqualified_move_preserves_manifest(project, map_id, slot, x, z):
    before = project.path.read_bytes()
    with pytest.raises(EditorError):
        project.move_placement(map_id, slot, x, z, 0)
    assert project.path.read_bytes() == before


def test_noop_reverse_and_composed_npc_collision(project):
    before = project.path.read_bytes()
    assert project.move_placement(5, 14, 565.5, 404.5, 0) == {"revision": 0, "changed": False}
    assert project.path.read_bytes() == before
    project.move_placement(5, 14, 564.5, 404.5, 0)
    changed = project.path.read_bytes()
    assert project.move_placement(5, 14, 564.5, 404.5, 1) == {"revision": 1, "changed": False}
    with pytest.raises(EditorError, match="blocked"):
        project.move_npc(1, 564, 404, 1)
    assert project.path.read_bytes() == changed
    project.move_npc(1, 565, 404, 1)
    with pytest.raises(EditorError, match="blocked"):
        project.move_placement(5, 14, 565.5, 404.5, 2)
    project.undo(2)
    project.move_placement(5, 14, 565.5, 404.5, 3)
    assert not project.diff()
    project.move_npc(1, 564, 404, 4)
    before = project.path.read_bytes()
    with pytest.raises(EditorError, match="blocked"):
        project.move_placement(5, 14, 564.5, 404.5, 5)
    assert project.path.read_bytes() == before


@pytest.mark.parametrize("field", ["record_before", "dependencies_sha256", "before", "after", "id"])
def test_authored_guards_and_undo_tampering_refused(project, field):
    project.move_placement(5, 14, 564.5, 404.5, 0)
    valid = copy.deepcopy(project.doc)
    invalid = copy.deepcopy(valid)
    invalid["placement_moves"]["5:14"][field] = "tampered"
    atomic_json(project.path, invalid)
    before = project.path.read_bytes()
    with pytest.raises(EditorError) as error:
        Project(project.root)
    assert error.value.code == "BEFORE_VALUE_MISMATCH"
    assert project.path.read_bytes() == before
    valid["history"].append({"operation": "placement.move", "positions": {}, "placement_moves": invalid["placement_moves"]})
    atomic_json(project.path, valid)
    before = project.path.read_bytes()
    with pytest.raises(EditorError):
        Project(project.root).undo(1)
    assert project.path.read_bytes() == before


def test_cli_shared_guard_and_concurrent_writers(project, tmp_path):
    prefix = [sys.executable, "-m", "sovereign_editor.cli"]
    commands = [prefix + ["move-placement", "--project", str(project.root), "--map", "5", "--slot", "14",
                          "--x", "564.5", "--z", "404.5", "--revision", "0"],
                prefix + ["move-npc", "--project", str(project.root), "--id", "1", "--x", "555", "--z", "399", "--revision", "0"]]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda cmd: subprocess.run(cmd, capture_output=True, text=True), commands))
    assert sorted(r.returncode for r in results) == [0, 2]
    assert next(json.loads(r.stdout)["error"]["code"] for r in results if r.returncode) == "STALE_REVISION"
    current = Project(project.root)
    old = current.doc["revision"] - 1
    for action in (lambda: current.move_placement(5, 14, 564.5, 404.5, old),
                   lambda: current.undo(old), lambda: current.export(tmp_path / "stale", old)):
        with pytest.raises(EditorError) as error:
            action()
        assert error.value.code == "STALE_REVISION"
    assert not (tmp_path / "stale").exists()
    moved = subprocess.run(commands[0][:-1] + ["1"], capture_output=True, text=True)
    assert moved.returncode == 0 and json.loads(moved.stdout)["ok"]
    query = subprocess.run(prefix + ["placement", "--project", str(project.root), "--map", "5", "--slot", "14"], capture_output=True, text=True)
    assert json.loads(query.stdout)["result"] == Project(project.root).placement(5, 14)


def test_widget_planter_move_cache_collision_reopen_undo(project):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from sovereign_editor.gui import EditorWindow
    app = QApplication.instance() or QApplication([])
    window = EditorWindow(project.root)
    window.show()
    window.select_placement(5, 14)
    app.processEvents()
    assert window.placement_move_button.isVisible() and window.placement_move_button.isEnabled()
    image_before = window.project.preview()["image"]
    window.placement_x_input.setValue(564.5)
    QTest.mouseClick(window.placement_move_button, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert Project(project.root).doc["placement_moves"]
    image_after = window.project.preview()["image"]
    assert image_before != image_after and Path(image_before).is_file()
    assert window.stock_scene.toImage() != __import__("PySide6").QtGui.QPixmap(image_before).toImage()
    assert window.scene_data["collision"][404 - 384][564 - 512]["blocked"]
    window.reload()
    app.processEvents()
    assert window.selected_placement == (5, 14) and window.placement_restore_button.isEnabled()
    QTest.mouseClick(window.undo_button, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert not Project(project.root).doc["placement_moves"]
    assert window.project.preview()["image"] == image_before
    window.select_placement(5, 12)
    assert not window.placement_move_button.isVisible()
    window.close()
    app.processEvents()
