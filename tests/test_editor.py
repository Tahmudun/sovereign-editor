"""Integration safety tests against a local, qualified ROM; no copyrighted fixture in Git."""
import copy
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from sovereign_editor.core import Project, atomic_json
from sovereign_editor.formats import EditorError, digest, resource


@pytest.fixture(scope="module")
def workspace(tmp_path_factory):
    source = Path(os.environ.get("SG_TEST_ROM", "projects/cherrygrove/baseline.nds"))
    if not source.is_file():
        pytest.skip("Set SG_TEST_ROM to a qualified local HeartGold ROM")
    root = tmp_path_factory.mktemp("editor-tests") / "project"
    p = Project.create(source, root)
    return p.root, copy.deepcopy(p.doc)


@pytest.fixture
def project(workspace):
    root, original = workspace
    atomic_json(root / "project.json", original)
    yield Project(root)
    atomic_json(root / "project.json", original)


def test_noop_export_is_exact(project, tmp_path):
    report = project.export(tmp_path / "noop", 0)
    assert report["changed_byte_count"] == 0
    assert (tmp_path / "noop/game.nds").read_bytes() == project.blob


def test_placed_model_coordinates_use_tiles(project):
    props = project.scene()["props"]
    # Stock west house and Pokemon Center anchors, read from their 16.16 records.
    assert (props[6]["tile_x"], props[6]["tile_z"]) == (36, 14.5)
    assert (props[4]["tile_x"], props[4]["tile_z"]) == (52.5, 6)
    assert max(p["tile_x"] for p in props) - min(p["tile_x"] for p in props) > 20


def test_move_export_readback_and_undo(project, tmp_path):
    import ndspy.narc
    import ndspy.rom
    project.move_npc(1, 555, 399, 0)
    # Reopen from disk, then decode export with ndspy independently of the writer.
    reopened = Project(project.root)
    assert reopened.doc["revision"] == 1
    report = reopened.export(tmp_path / "edited", 1)
    edited = (tmp_path / "edited/game.nds").read_bytes()
    original = ndspy.rom.NintendoDSRom(project.blob)
    candidate = ndspy.rom.NintendoDSRom(edited)
    changed_ids = [i for i, (a, b) in enumerate(zip(original.files, candidate.files)) if a != b]
    assert changed_ids == [original.filenames.idOf("a/0/3/2")]
    a = ndspy.narc.NARC(original.getFileByName("a/0/3/2"))
    b = ndspy.narc.NARC(candidate.getFileByName("a/0/3/2"))
    assert [i for i, (x, y) in enumerate(zip(a.files, b.files)) if x != y] == [64]
    assert [(i, x, y) for i, (x, y) in enumerate(zip(a.files[64], b.files[64])) if x != y] == [(144, 42, 43)]
    offset = report["patches"][0]["rom_offset"]
    assert edited[:offset] == project.blob[:offset]
    assert edited[offset + 1:] == project.blob[offset + 1:]
    assert report["changed_byte_count"] == 1
    reopened.undo(1)
    undone = reopened.export(tmp_path / "undone", 2)
    assert undone["candidate_sha256"] == digest(project.blob)


@pytest.mark.parametrize("x,z", [(543, 399), (576, 399), (555, 416), (True, 399), (555.5, 399), (544, 384), (555, 392), (566, 399)])
def test_invalid_move_never_persists(project, x, z):
    before = project.path.read_bytes()
    with pytest.raises(EditorError):
        project.move_npc(1, x, z, 0)
    assert project.path.read_bytes() == before


def test_other_actors_locked(project):
    with pytest.raises(EditorError, match="Only NPC 1"):
        project.move_npc(0, 555, 399, 0)


def test_stale_revision_rejected(project):
    other = Project(project.root)
    project.move_npc(1, 555, 399, 0)
    with pytest.raises(EditorError) as error:
        other.move_npc(1, 554, 399, 0)
    assert error.value.code == "STALE_REVISION"
    assert Project(project.root).doc["positions"]["1"]["x"] == 555


def test_identical_operation_reuses_revision(project):
    project.move_npc(1, 555, 399, 0)
    result = project.move_npc(1, 555, 399, 1)
    assert result == {"revision": 1, "changed": False}
    assert len(project.doc["history"]) == 1


def test_export_refuses_existing_folder(project, tmp_path):
    with pytest.raises(EditorError) as error:
        project.export(tmp_path, 0)
    assert error.value.code == "EXISTS"


def test_baseline_tampering_rejected(project):
    path = project.root / "baseline.nds"
    original = path.read_bytes()[:1]
    try:
        with path.open("r+b") as f:
            f.write(bytes([original[0] ^ 1]))
        with pytest.raises(EditorError) as error:
            Project(project.root)
        assert error.value.code == "BASELINE_CHANGED"
    finally:
        with path.open("r+b") as f:
            f.write(original)


def test_unsupported_map_is_rejected_before_creation(project, tmp_path):
    data = bytearray(project.blob)
    offset, _ = resource(data, "a/0/6/5", 5)
    data[offset + 50] ^= 1
    source = tmp_path / "changed.nds"
    source.write_bytes(data)
    with pytest.raises(EditorError) as error:
        Project.create(source, tmp_path / "unsupported")
    assert error.value.code == "UNSUPPORTED_ROM"
    assert not (tmp_path / "unsupported").exists()


def test_authored_unknown_operation_rejected(project):
    doc = copy.deepcopy(project.doc)
    doc["positions"]["0"] = {"x": 555, "z": 399}
    atomic_json(project.path, doc)
    with pytest.raises(EditorError):
        Project(project.root)


def test_cli_json_and_concurrent_writer_guard(project):
    command = [sys.executable, "-m", "sovereign_editor.cli", "move-npc", "--project", str(project.root),
               "--id", "1", "--x", "555", "--z", "399", "--revision", "0"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: subprocess.run(command, capture_output=True, text=True), range(2)))
    assert sorted(r.returncode for r in results) == [0, 2]
    errors = [json.loads(r.stdout) for r in results if r.returncode == 2]
    assert errors[0]["error"]["code"] == "STALE_REVISION"


def test_save_is_copied_without_modification(project, tmp_path):
    save = tmp_path / "source.sav"
    contents = b"\x42" * 524288
    save.write_bytes(contents)
    result = project.export(tmp_path / "paired", 0, save)
    assert (tmp_path / "paired/game.sav").read_bytes() == save.read_bytes() == contents
    assert result["save_sha256"] == digest(contents)


def test_native_widget_apply_undo_drag_and_reopen(project, tmp_path):
    # In-process offscreen widget test: no host desktop/emulator automation.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt, QPoint
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from sovereign_editor.gui import EditorWindow, STYLE
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window = EditorWindow(project.root)
    window.show()
    app.processEvents()
    assert window.project is not None and len(window.actors) == 6
    window.select_npc(1)
    window.x_input.setValue(555)
    QTest.mouseClick(window.apply_button, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert Project(project.root).doc["positions"]["1"] == {"x": 555, "z": 399}
    QTest.mouseClick(window.undo_button, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert not Project(project.root).doc["positions"]
    actor = window.actors[1]
    start = window.view.mapFromScene(actor.pos())
    finish = window.view.mapFromScene(actor.pos() + __import__('PySide6').QtCore.QPointF(24, 0))
    QTest.mousePress(window.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    QTest.mouseMove(window.view.viewport(), finish, 50)
    QTest.mouseRelease(window.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, finish)
    app.processEvents()
    assert Project(project.root).doc["positions"]["1"]["x"] == 555
    window.reload()
    app.processEvents()
    assert window.x_input.value() == 555
    window.toggles["Grid"].setChecked(True)
    app.processEvents()
    evidence = tmp_path
    (evidence / "m4").mkdir(exist_ok=True)
    assert window.grab().save(str(evidence / "m4" / "editor-regression.png"))
    window.close()
    app.processEvents()


def test_stock_placements_resolve_with_embedded_textures(project):
    before = project.path.read_bytes()
    report = project.placements()
    assert (report["count"], report["unique_models"]) == (15, 9)
    assert len({p["key"] for p in report["placements"]}) == 15
    assert {p["model_id"] for p in report["placements"]} == {15, 29, 30, 37, 38, 39, 40, 50, 52}
    assert all(p["rotation_degrees"] == [0, 0, 0] and p["scale"] == [1, 1, 1]
               and p["editable"] == (p["key"] == "5:14") and p["unknown_hex"] == "00" * 8 for p in report["placements"])
    assert sum(m["summary"]["triangles"] for m in project.stock_models().values()) == 770
    assert all(primitive.texture is not None for m in project.stock_models().values() for primitive in m["primitives"])
    center = project.placement(5, 4)
    assert center["model_name"] == "pc" and center["model_source"]["member"] == 40
    assert center["global_position"] == [564.5, 1, 390]
    assert center["dependencies"]["nearby_doors"] == [0]
    assert center["dependencies"]["status"] == "unqualified for placement writes"
    assert project.path.read_bytes() == before
    with pytest.raises(EditorError, match="No placement"):
        project.placement(4, 99)


@pytest.mark.parametrize("map_id,name", [(4, "map16_12c.glb"), (5, "map17_12c.glb")])
def test_native_decoder_matches_independent_terrain_glb(project, map_id, name):
    import numpy as np
    from sovereign_editor.formats import ASSETS
    from sovereign_editor.nitro import decode_model
    from sovereign_editor.preview import glb
    raw = project.maps[map_id][2]
    summary, primitives = decode_model(raw, geometry_only=True)
    doc, access = glb(ASSETS / name)
    native = np.concatenate([p.vertices for p in primitives])
    reference = np.concatenate([access(p["attributes"]["POSITION"]) for p in doc["meshes"][0]["primitives"]])
    assert {tuple(v) for v in native} == {tuple(v) for v in reference}
    assert summary["triangles"] == sum(len(access(p["indices"])) // 3 for p in doc["meshes"][0]["primitives"])


def test_stock_model_resource_tampering_refused(project, tmp_path):
    blob = bytearray(project.blob)
    offset, _ = resource(blob, "a/0/4/0", 40)
    blob[offset + 100] ^= 1
    source = tmp_path / "altered-model.nds"
    source.write_bytes(blob)
    with pytest.raises(EditorError, match="Unsupported modified resource"):
        Project.create(source, tmp_path / "unsupported-model")
    assert not (tmp_path / "unsupported-model").exists()


def test_unqualified_rotation_and_unknown_scene_command_refused(project):
    import struct
    from sovereign_editor.nitro import decode_model, info, blocks
    project.maps[5][1][0]["rotation_raw"] = [0, 1, 0]
    with pytest.raises(EditorError, match="neutral placement transforms"):
        project.placements()
    _, raw = resource(project.blob, "a/0/4/0", 40)
    data = bytearray(raw)
    mdl_offset = struct.unpack_from("<I", data, 16)[0]
    model_offset = struct.unpack("<I", info(blocks(raw)[b"MDL0"], 8)[0][1])[0]
    sbc = struct.unpack_from("<I", data, mdl_offset + model_offset + 4)[0]
    data[mdl_offset + model_offset + sbc] = 0xff
    with pytest.raises(EditorError, match="Unsupported Nitro scene command"):
        decode_model(bytes(data))


def test_preview_cache_and_rendering_preserve_project(project, monkeypatch):
    from sovereign_editor import preview
    from PIL import Image
    before = project.path.read_bytes()
    full = Path(project.preview()["image"])
    stat = full.stat().st_mtime_ns
    assert Path(project.preview()["image"]) == full and full.stat().st_mtime_ns == stat
    bare = Path(project.preview(include_models=False)["image"])
    assert full != bare and Image.open(full).size == (1536, 768)
    assert Image.open(full).tobytes() != Image.open(bare).tobytes()
    monkeypatch.setattr(preview, "RENDERER_VERSION", "test-renderer-change")
    changed = Path(project.preview()["image"])
    assert changed != full and changed.is_file() and full.is_file()
    assert project.path.read_bytes() == before


def test_cli_placement_query_matches_core(project):
    result = subprocess.run([sys.executable, "-m", "sovereign_editor.cli", "placement", "--project", str(project.root),
                             "--map", "5", "--slot", "4"], capture_output=True, text=True)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"ok": True, "result": project.placement(5, 4)}


def test_widget_placement_selection_is_read_only(project):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QGraphicsItem
    from PySide6.QtTest import QTest
    from sovereign_editor.gui import EditorWindow
    app = QApplication.instance() or QApplication([])
    before = project.path.read_bytes()
    window = EditorWindow(project.root)
    window.show()
    app.processEvents()
    assert len(window.placement_items) == 15
    window.focus_east()
    item = window.placement_items[(5, 4)]
    point = window.view.mapFromScene(item.rect().center())
    QTest.mouseClick(window.view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)
    app.processEvents()
    assert window.selected_placement == (5, 4)
    assert not item.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsMovable
    assert not window.apply_button.isEnabled() and not window.model_image.pixmap().isNull()
    window.resize(1050, 650)
    app.processEvents()
    assert window.model_image.pixmap().width() <= window.model_image.width()
    assert window.model_image.mapTo(window, window.model_image.rect().topRight()).x() < window.width()
    window.move_selected(555, 399)
    window.toggles["Collision"].setChecked(True)
    window.toggles["Buildings"].setChecked(False)
    assert not window.placement_items
    window.toggles["Buildings"].setChecked(True)
    assert len(window.placement_items) == 15
    window.reload()
    assert window.selected_placement == (5, 4)
    window.select_npc(1)
    assert window.apply_button.isEnabled() and not window.model_image.isVisible()
    assert project.path.read_bytes() == before
    window.close()
    app.processEvents()
