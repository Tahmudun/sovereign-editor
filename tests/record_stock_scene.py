"""Capture this application's widgets and model coverage; never drive an emulator.

Default: offscreen. --native explicitly runs the same Qt window on Cocoa.
"""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
parser = argparse.ArgumentParser()
parser.add_argument("--native", action="store_true")
parser.add_argument("--keep-open", action="store_true")
args = parser.parse_args()
os.environ["QT_QPA_PLATFORM"] = "cocoa" if args.native else "offscreen"

from PIL import Image, ImageDraw
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from sovereign_editor.gui import EditorWindow, STYLE
from sovereign_editor.formats import digest

output = Path("evidence/m2")
output.mkdir(exist_ok=True)
app = QApplication([])
app.setApplicationName("Sovereign Editor")
app.setStyle("Fusion")
app.setStyleSheet(STYLE)
window = EditorWindow("projects/cherrygrove")
assert window.project is not None, window.message.text()
before = window.project.path.read_bytes()
window.show()


def capture():
    try:
        platform = app.platformName()
        assert platform == ("cocoa" if args.native else "offscreen")
        placements = window.project.placements()["placements"]
        models = window.project.stock_models()
        counts = Counter(p["model_id"] for p in placements)
        contact = Image.new("RGB", (900, 660), "#182229")
        draw = ImageDraw.Draw(contact)
        for i, (model_id, model) in enumerate(models.items()):
            placement = next(p for p in placements if p["model_id"] == model_id)
            preview = window.project.placement_preview(placement["map"], placement["slot"])
            x, y = (i % 3) * 300, (i // 3) * 220
            contact.paste(Image.open(preview), (x + 10, y))
            draw.text((x + 14, y + 184), f"{model_id} · {model['summary']['name']} · {counts[model_id]} placement(s)", fill="#e8edf1")
        contact.save(output / "stock-models-contact.png")
        checked = []
        for p in placements:
            window.select_placement(p["map"], p["slot"])
            assert not window.model_image.pixmap().isNull() and not window.apply_button.isEnabled()
            checked.append(p["key"])
        window.select_npc(1)
        app.processEvents()
        window.fit_map()
        assert window.grab().save(str(output / f"{platform}-overview.png"))
        window.select_placement(5, 4)
        app.processEvents()
        window.focus_east()
        assert window.grab().save(str(output / f"{platform}-center.png"))
        window.toggles["Collision"].setChecked(True)
        window.toggles["Triggers"].setChecked(True)
        assert window.grab().save(str(output / f"{platform}-overlays.png"))
        window.toggles["Collision"].setChecked(False)
        window.toggles["Triggers"].setChecked(False)
        window.resize(1050, 650)
        app.processEvents()
        window.focus_east()
        assert window.grab().save(str(output / f"{platform}-compact.png"))
        window.resize(1440, 900)
        app.processEvents()
        window.focus_east()
        assert window.project.path.read_bytes() == before
        (output / f"{platform}-review.json").write_text(json.dumps({
            "platform": platform, "placements_inspected": checked, "models_resolved": len(models),
            "project_sha256": digest(before), "project_unchanged": True,
            "emulator_automation": False, "capture": "in-process Qt window only"
        }, indent=2) + "\n")
        print(json.dumps({"platform": platform, "placements": len(checked), "models": len(models),
                          "project_unchanged": True, "evidence": str(output), "left_open": args.keep_open}), flush=True)
    except Exception:
        import traceback
        traceback.print_exc()
        app.exit(1)
        return
    if not args.keep_open:
        window.close()
        app.quit()


QTimer.singleShot(500, capture)
sys.exit(app.exec())
