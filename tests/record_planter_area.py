"""M4 review of this editor's Qt widgets; never desktop/emulator automation."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
parser = argparse.ArgumentParser()
parser.add_argument("--native", action="store_true")
args = parser.parse_args()
os.environ["QT_QPA_PLATFORM"] = "cocoa" if args.native else "offscreen"

from PySide6.QtCore import Qt, QTimer, QRectF
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QGraphicsItem
from sovereign_editor.core import Project, atomic_json
from sovereign_editor.formats import digest
from sovereign_editor.gui import EditorWindow, STYLE, TILE
from test_planter_area import drag_planter

output = Path("evidence/m4").resolve()
protected = Path("projects/cherrygrove/project.json")
before = protected.read_bytes()
temporary = tempfile.TemporaryDirectory(prefix="ui-review-", dir=output)
project = Project.create("projects/cherrygrove/baseline.nds", Path(temporary.name) / "project", "M4 planter review")
legacy = json.loads(Path("evidence/m4/before/projects/cherrygrove/project.json").read_text())
legacy["name"] = "M4 planter review"
atomic_json(project.path, legacy)
app = QApplication([])
app.setApplicationName("Sovereign Editor")
app.setStyle("Fusion")
app.setStyleSheet(STYLE)
window = EditorWindow(project.root)
window.show()


def review():
    try:
        platform = app.platformName()
        assert platform == ("cocoa" if args.native else "offscreen")
        window.select_placement(5, 14)
        window.toggles["Grid"].setChecked(True)
        window.toggles["Collision"].setChecked(True)
        window.toggles["Triggers"].setChecked(True)
        window.focus_east()
        app.processEvents()
        assert window.grab().save(str(output / f"{platform}-before.png"))
        drag_planter(window, -1.1, 1.15)
        assert Project(project.root).placement(5, 14)["global_position"] == [563.5, 1, 405.5]
        assert window.grab().save(str(output / f"{platform}-after.png"))
        before_refusal = project.path.read_bytes()
        drag_planter(window, 2, 0)
        assert "Unqualified target" in window.message.text()
        assert project.path.read_bytes() == before_refusal
        assert window.grab().save(str(output / f"{platform}-refused.png"))
        window.placement_x_input.setValue(564.5)
        window.placement_z_input.setValue(404.5)
        QTest.mouseClick(window.placement_move_button, Qt.MouseButton.LeftButton)
        app.processEvents()
        window.undo()
        window.reload()
        app.processEvents()
        assert window.placement_x_input.value() == 563.5 and window.placement_z_input.value() == 405.5
        for p in window.placement_data:
            window.select_placement(p["map"], p["slot"])
            assert window.placement_fields.isVisible() == p["editable"]
            assert bool(window.placement_items[(p["map"], p["slot"])].flags() & QGraphicsItem.GraphicsItemFlag.ItemIsMovable) == p["editable"]
        window.select_placement(5, 14)
        # All five anchors receive a close static visual/collision review.
        for i, anchor in enumerate(window.project.decoration_proof["allowed_anchors"]):
            window.placement_x_input.setValue(anchor["x"])
            window.placement_z_input.setValue(anchor["z"])
            QTest.mouseClick(window.placement_move_button, Qt.MouseButton.LeftButton)
            app.processEvents()
            window.view.fitInView(QRectF(48*TILE, 17*TILE, 10*TILE, 9*TILE), Qt.AspectRatioMode.KeepAspectRatio)
            assert window.view.grab().save(str(output / f"{platform}-anchor-{i}.png"))
        window.focus_east()
        for key in ("Grid", "Collision", "Triggers"):
            window.toggles[key].setChecked(False)
        app.processEvents()
        assert window.grab().save(str(output / f"{platform}-scene.png"))
        window.resize(1050, 650)
        app.processEvents()
        window.focus_east()
        assert window.grab().save(str(output / f"{platform}-compact.png"))
        assert window.placement_move_button.mapTo(window, window.placement_move_button.rect().bottomRight()).x() < window.width()
        assert window.placement_z_input.mapTo(window, window.placement_z_input.rect().bottomRight()).x() < window.width()
        assert Project(project.root).doc["positions"] == {"1": {"x": 555, "z": 399}}
        assert protected.read_bytes() == before
        report = {"platform": platform, "qualified_placement": "5:14", "anchors_captured": 5,
                  "fractional_drag_snaps": True, "unqualified_drag_refused_and_restored": True,
                  "inspector_reopen_mixed_legacy_undo": True, "other_14_placements_read_only": True,
                  "compact_controls_fit": True, "main_project_preserved": True,
                  "main_project_sha256": digest(before), "emulator_automation": False,
                  "capture": "in-process Qt widgets", "native_game_acceptance": "pending; guide scene untested"}
        (output / f"{platform}-review.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report), flush=True)
        window.close()
        app.quit()
    except Exception:
        import traceback
        traceback.print_exc()
        app.exit(1)


QTimer.singleShot(500, review)
status = app.exec()
temporary.cleanup()
sys.exit(status)
