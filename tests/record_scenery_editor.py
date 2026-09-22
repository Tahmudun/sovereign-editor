"""Explicit styled Qt review of scenery authoring. Uses a disposable project."""
import json
import os
from pathlib import Path

from sovereign_editor.core import Project, atomic_json


def test_record(tmp_path):
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtWidgets import QApplication
    from sovereign_editor.gui import STYLE
    from sovereign_editor.map_inspector import MapInspectorWindow
    app=QApplication.instance() or QApplication([])
    app.setStyle('Fusion');app.setStyleSheet(STYLE)
    p=Project.create('projects/map-adjacent-1/baseline.nds',tmp_path/'project')
    atomic_json(p.path,json.loads(Path('projects/map-adjacent-1/project.json').read_text()))
    w=MapInspectorWindow(Project(p.root),context=(67,[17,12]));w.show();app.processEvents()
    out=Path('evidence/map-scenery-1/screens');out.mkdir(exist_ok=True)
    w.open_palette();app.processEvents()
    assert w.palette_dialog.grab().save(str(out/'01-palette.png'))
    w.palette_dialog.close()
    w.select_placement(12);w.start_scenery('duplicate');w.x_input.set_anchor(553.5)
    w.move_collision.setChecked(True)
    w.selected_cells={(551,z) for z in (397,398,399)}
    w.preview();w.focus_tile(552,398,12);app.processEvents()
    assert w.scenery_scenes and w.apply_button.isEnabled()
    assert w.grab().save(str(out/'02-duplicate-preview.png'))
    w.apply_transaction();assert len(w.view_data['placements'])==16
    # Added object remains selected after Apply; its raw identity is reused by transfer.
    w.destination_box.setCurrentIndex(next(i for i in range(w.destination_box.count())
        if w.destination_box.itemData(i)=={'header':33,'cell':[18,12]}))
    w.start_scenery('transfer');w.x_input.set_anchor(578.5);w.z_input.set_anchor(398.5)
    w.selected_cells={(553,z) for z in (397,398,399)}
    w.preview();w.focus_tile(567,398,36);app.processEvents()
    assert len(w.scenery_scenes)==2
    assert w.grab().save(str(out/'03-transfer-preview.png'))
    w.apply_transaction();assert w.header==33
    w.resize(1180,760);w.neighbors_toggle.setChecked(False);w.focus_tile(578.5,398.5,12)
    app.processEvents();assert w.grab().save(str(out/'04-saved-compact.png'))
    w.start_scenery('delete')
    w.selected_cells=set();w.move_collision.setChecked(False)
    for z in (397,398,399):w._stage_flag(578,z,False)
    w.preview();app.processEvents()
    assert w.grab().save(str(out/'05-remove-preview.png'))
    w.cancel_staged();w.close();app.processEvents()
