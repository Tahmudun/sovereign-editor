"""Species/area forms and ordinary trainer fields drive the same Project operations as the CLI."""
import json
import os
from pathlib import Path
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from sovereign_editor.core import Project
from sovereign_editor.gameplay_ui import GameplayEditor
from sovereign_editor.gui import STYLE

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT/'projects/scyther-orchestration-1/baseline.nds'
OUT = ROOT/'evidence/gameplay-authoring-v1/visual'


def app():
    a = QApplication.instance() or QApplication([]); a.setStyle('Fusion'); a.setStyleSheet(STYLE); return a


def machine(w, index):
    return next(w.machines.item(i) for i in range(w.machines.count()) if w.machines.item(i).data(Qt.ItemDataRole.UserRole) == index)


def test_species_and_area_drafts_preview_apply_undo(tmp_path):
    if not BASELINE.exists():
        pytest.skip('Pinned baseline is local and untracked')
    a = app(); OUT.mkdir(parents=True, exist_ok=True)
    p = Project.create(BASELINE, tmp_path/'project')
    w = GameplayEditor(p); w.show(); a.processEvents(); before = p.path.read_bytes()
    assert w.header == 34 and w.trainer['id'] == 47 and w.area.findData(33) >= 0
    w.tabs.setCurrentIndex(2); w.species.setCurrentIndex(w.species.findData(161)); a.processEvents()
    assert w.stats['hp'].value() == 35 and w.learnset.rowCount() == 13
    w.stats['hp'].setValue(40); w.types[1].setCurrentIndex(w.types[1].findData(4))
    w.abilities[0].setCurrentIndex(w.abilities[0].findData(51)); w.abilities[1].setCurrentIndex(w.abilities[1].findData(50))
    w.growth.setCurrentIndex(w.growth.findData(4))
    w.append_learn({'level': 5, 'move': 98}, True)
    w.evolutions.cellWidget(0, 1).setValue(6)
    assert not w.evolutions.cellWidget(1, 1).isEnabled() or w.evolutions.cellWidget(1, 2).currentData() == 0
    w.species.setCurrentIndex(w.species.findData(16)); a.processEvents()
    w.stats['speed'].setValue(60); machine(w, 16).setCheckState(Qt.CheckState.Unchecked)
    w.species.setCurrentIndex(w.species.findData(161)); a.processEvents()
    assert w.stats['hp'].value() == 40 and w.learnset.rowCount() == 14 and w.evolutions.cellWidget(0, 1).value() == 6
    # Another area's encounters join the same atomic batch.
    w.area.setCurrentIndex(w.area.findData(33)); a.processEvents()
    assert w.header == 33 and not w.tabs.isTabEnabled(0) and w.tabs.isTabEnabled(1)
    w.tabs.setCurrentIndex(1); w.wild.cellWidget(0, 1).setValue(3)
    w.preview_changes(); assert w.apply_button.isEnabled() and p.path.read_bytes() == before
    kinds = sorted((o['kind'], o.get('id', o.get('header'))) for o in w.pending['operations'])
    assert kinds == [('encounters', 33), ('species', 16), ('species', 161)]
    text = w.preview.toPlainText()
    assert 'Impact' in text and 'evolutions' in text and 'TM017' in text and 'advisory' in text
    w.tabs.setCurrentIndex(2); a.processEvents(); assert w.grab().save(str(OUT/'ui-species.png'))
    w.stats['speed'].setValue(26); assert not w.apply_button.isEnabled()
    w.stats['speed'].setValue(20); w.preview_changes(); w.apply()
    assert p.doc['revision'] == 1 and len(p.doc['history']) == 1
    q = Project(p.root)
    assert q.gameplay_species(161)['personal']['types'] == [0, 4] and {'level': 5, 'move': 98} in q.gameplay_species(161)['learnset']
    assert 16 not in q.gameplay_species(16)['machines']['base_compatible']
    assert q.gameplay_data(33)['encounters']['methods']['grass']['levels'][0] == 3
    w.undo(); assert p.doc['map_edits'] == []
    w.redo(); assert p.doc['revision'] == 3
    w.area.setCurrentIndex(w.area.findData(34)); a.processEvents(); assert w.grab().save(str(OUT/'ui-area-r30.png'))
    w.close(); a.processEvents()


def test_story_trainer_panel_stages_an_ordinary_trainer(tmp_path):
    if not BASELINE.exists():
        pytest.skip('Pinned baseline is local and untracked')
    from sovereign_editor.map_inspector import MapInspectorWindow
    from sovereign_editor.story_ui import StoryEditor
    a = app(); OUT.mkdir(parents=True, exist_ok=True)
    p = Project.create(BASELINE, tmp_path/'project'); ctx = {'header': 33, 'cell': [18, 12]}
    package = json.loads((ROOT/'tests/fixtures/tiana.character.json').read_text())
    p.apply_area_edit(0, operations=[{'kind': 'story', 'context': ctx, 'request': {'kind': 'character', 'key': 'tiana', 'value': package}},
                                     {'kind': 'story', 'context': ctx, 'request': {'kind': 'state', 'key': 'robin_defeated', 'value': {'name': 'Robin defeated'}}}])
    w = MapInspectorWindow(p, context=(33, [18, 12])); w.show(); a.processEvents(); d = StoryEditor(w); d.show(); a.processEvents()
    d.tabs.setCurrentIndex(1); a.processEvents()
    d.trainer_key.setText('robin'); d.trainer_name.setText('Robin')
    d.trainer_character.setCurrentIndex(d.trainer_character.findData('stock:3'))
    d.trainer_policy.setCurrentIndex(1); d.trainer_defeat.setCurrentIndex(d.trainer_defeat.findData('robin_defeated'))
    d.trainer_custom.setChecked(True); d.party.setRowCount(0)
    d.party_row({'species': 16, 'level': 4, 'moves': [16, 33, 28, 0], 'held_item': 0})
    d.party_row({'species': 19, 'level': 5, 'moves': [98, 33, 39, 0], 'held_item': 155})
    assert d.party.cellWidget(0, 2).isEnabled() and d.party.cellWidget(1, 6).currentData() == 155
    d.trainer_before.setPlainText('Here I come!'); d.trainer_after.setPlainText('Good battle!'); d.trainer_revisit.setPlainText('You already won.')
    before = p.path.read_bytes(); d.stage_trainer(); a.processEvents()
    assert d.apply_button.isEnabled() and p.path.read_bytes() == before
    staged = d.preview.story_library()['trainers']['robin']
    assert staged['policy'] == 'ordinary-single-v1' and staged['party'][1]['held_item'] == 155 and staged['revisit'] == ['You already won.']
    assert d.grab().save(str(OUT/'ui-ordinary-trainer.png'))
    d.trainer_policy.setCurrentIndex(0); a.processEvents()
    assert not d.party.cellWidget(0, 2).isEnabled() and 'Practice' in d.policy_note.text()
    d.trainer_policy.setCurrentIndex(1); d.apply()
    assert Project(p.root).story_library()['trainers']['robin']['defeat_state'] == 'robin_defeated'
    w.close(); a.processEvents()
