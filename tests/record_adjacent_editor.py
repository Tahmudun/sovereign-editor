"""Explicit visual QA recorder; scratch project only, no native acceptance claim."""
import json
import os
from pathlib import Path

from sovereign_editor.core import Project, atomic_json


def test_record(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from sovereign_editor.gui import STYLE
    from sovereign_editor.map_inspector import MapInspectorWindow
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    p = Project.create("projects/map-authoring-1/baseline.nds", tmp_path / "project")
    atomic_json(p.path, json.loads(Path("projects/map-authoring-1/project.json").read_text()))
    p = Project(p.root)
    out = Path("evidence/map-adjacent-1/screens")
    out.mkdir(parents=True, exist_ok=True)
    w = MapInspectorWindow(p, context=(67, [17, 12]))
    w.show()
    app.processEvents()
    w.neighbors_toggle.setChecked(True)
    app.processEvents()
    assert w.grab().save(str(out / "01-cherrygrove-neighbors.png"))
    assert w.activate_neighbor(48, 16)
    app.processEvents()
    w.fit_map()
    w.set_tool("block")
    w.paint_tile(0, 14)
    app.processEvents()
    assert w.grab().save(str(out / "02-route29-active-boundary.png"))
    w.cancel_staged()
    w.set_tool("select")
    w.load_context(60, [21, 12])
    app.processEvents()
    w.select_placement(13)
    w.align_sign.setChecked(True)
    w.preview()
    w.focus_tile(685, 400, 12)
    app.processEvents()
    assert w.grab().save(str(out / "03-town-sign-repair-preview.png"))
    w.apply_transaction()
    w.select_placement(13)
    w.resize(1180, 760)
    app.processEvents()
    w.focus_tile(685, 400, 12)
    app.processEvents()
    assert p.map_sign(header=60, cell=[21, 12])["position"] == [684, 400]
    assert w.grab().save(str(out / "04-town-sign-saved-compact.png"))
    w.close()
    app.processEvents()
