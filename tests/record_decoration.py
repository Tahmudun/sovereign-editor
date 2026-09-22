"""Review this editor's own widgets. No desktop or emulator automation."""
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

from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from sovereign_editor.core import Project
from sovereign_editor.formats import digest
from sovereign_editor.gui import EditorWindow, STYLE

output = Path("evidence/m3").resolve()
protected = Path("projects/cherrygrove/project.json")
before = protected.read_bytes()
temporary = tempfile.TemporaryDirectory(prefix="ui-review-", dir=output)
project = Project.create("projects/cherrygrove/baseline.nds", Path(temporary.name) / "project", "M3 planter review")
project.move_npc(1, 555, 399, 0)
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
        QTest.mouseClick(window.placement_move_button, Qt.MouseButton.LeftButton)
        app.processEvents()
        assert Project(project.root).doc["placement_moves"]
        assert window.grab().save(str(output / f"{platform}-after.png"))
        window.toggles["Grid"].setChecked(False)
        window.toggles["Collision"].setChecked(False)
        window.toggles["Triggers"].setChecked(False)
        app.processEvents()
        assert window.grab().save(str(output / f"{platform}-scene.png"))
        window.reload()
        app.processEvents()
        assert window.selected_placement == (5, 14)
        window.resize(1050, 650)
        app.processEvents()
        window.focus_east()
        assert window.grab().save(str(output / f"{platform}-compact.png"))
        assert window.placement_move_button.isEnabled()
        assert window.placement_move_button.mapTo(window, window.placement_move_button.rect().bottomRight()).x() < window.width()
        window.undo()
        app.processEvents()
        assert not Project(project.root).doc["placement_moves"]
        assert Project(project.root).doc["positions"] == {"1": {"x": 555, "z": 399}}
        for p in window.placement_data:
            window.select_placement(p["map"], p["slot"])
            assert window.placement_move_button.isVisible() == (p["key"] == "5:14")
        assert protected.read_bytes() == before
        report = {"platform": platform, "qualified_placement": "5:14", "apply_reopen_undo": True,
                  "other_14_placements_read_only": True, "compact_button_fits": True,
                  "main_project_preserved": True, "main_project_sha256": digest(before),
                  "emulator_automation": False, "capture": "in-process Qt widgets"}
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
