import copy
import os
from pathlib import Path
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication
from sovereign_editor.core import Project
from sovereign_editor.gameplay_ui import GameplayEditor
from sovereign_editor.gui import STYLE


def test_forms_keep_drafts_preview_discard_atomic_apply_stale_and_undo(tmp_path):
    app=QApplication.instance() or QApplication([]);app.setStyle('Fusion');app.setStyleSheet(STYLE)
    p=Project.create('projects/scyther-orchestration-1/baseline.nds',tmp_path/'project')
    w=GameplayEditor(p);w.show();app.processEvents();before=p.path.read_bytes()
    assert w.trainer['id']==47 and w.team.rowCount()==2
    w.team.cellWidget(0,1).setValue(4)
    w.trainers.setCurrentIndex(w.trainers.findData(249));w.trainers.setCurrentIndex(w.trainers.findData(47))
    assert w.team.cellWidget(0,1).value()==4
    w.tabs.setCurrentIndex(1);w.wild.cellWidget(0,1).setValue(3)
    w.time.setCurrentIndex(1);assert w.wild.cellWidget(0,1).value()==3
    w.wild.cellWidget(0,2).setCurrentIndex(w.wild.cellWidget(0,2).findData(16))
    w.method.setCurrentIndex(1);w.method.setCurrentIndex(0);assert w.wild.cellWidget(0,2).currentData()==16
    w.preview_changes();assert w.apply_button.isEnabled() and p.path.read_bytes()==before
    assert len(w.pending['operations'])==2 and '100 → 3' in w.preview.toPlainText()
    w.rate.setValue(20);assert not w.apply_button.isEnabled()
    w.preview_changes();w.apply();assert p.doc['revision']==1 and len(p.doc['history'])==1
    d=Project(p.root).gameplay_data();assert d['encounters']['methods']['grass']['levels'][0]==3
    w.undo();assert p.doc['revision']==2 and p.doc['map_edits']==[]
    w.redo();assert p.doc['revision']==3
    saved=p.path.read_bytes();w.team.cellWidget(0,1).setValue(7);w.reload();assert p.path.read_bytes()==saved
    assert w.team.cellWidget(0,1).value()==4
    w.trainers.setCurrentIndex(w.trainers.findData(8));assert not w.team.isEnabled()
    w.trainers.setCurrentIndex(w.trainers.findData(47));w.team.cellWidget(0,1).setValue(6);w.preview_changes()
    Project(p.root).undo(3);w.guard(w.apply);assert 'revision' in w.status.text().lower() and not w.apply_button.isEnabled()
    w.close();app.processEvents()


def test_named_custom_moves_and_all_methods_screenshot(tmp_path):
    app=QApplication.instance() or QApplication([]);app.setStyle('Fusion');app.setStyleSheet(STYLE)
    p=Project.create('projects/scyther-orchestration-1/baseline.nds',tmp_path/'project')
    w=GameplayEditor(p);w.show();app.processEvents();w.custom.setChecked(True)
    for row,moves in enumerate([[16,33,28,0],[98,33,39,0]]):
        for i,move in enumerate(moves):
            cell=w.team.cellWidget(row,i+2);cell.setCurrentIndex(cell.findData(move))
    cell=w.team.cellWidget(1,6);cell.setCurrentIndex(cell.findData(155))
    w.preview_changes();assert w.apply_button.isEnabled()
    out=Path('evidence/gameplay-data-1');out.mkdir(exist_ok=True)
    assert w.grab().save(str(out/'ui-trainer.png'))
    w.tabs.setCurrentIndex(1);w.wild.cellWidget(0,1).setValue(4);w.time.setCurrentIndex(2)
    w.preview_changes();app.processEvents();assert w.grab().save(str(out/'ui-grass.png'))
    w.method.setCurrentIndex(1);app.processEvents();assert w.wild.columnCount()==4 and not w.time.isEnabled()
    assert w.grab().save(str(out/'ui-surf.png'))
    for index in range(1,6):
        w.method.setCurrentIndex(index);assert w.wild.rowCount()==(2 if index==2 else 5)
    w.close();app.processEvents()
