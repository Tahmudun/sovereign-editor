"""Native gameplay forms; preview/apply use the same Project API as the CLI.

Areas come from Project.gameplay_areas (compatible-area discovery). Drafts for
teams, encounters (per area) and species survive navigation and are previewed
and applied together as one atomic, undoable Project transaction.
"""
import copy
from collections import Counter
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QTabWidget, QWidget, QComboBox, QSpinBox, QCheckBox, QTableWidget, QPlainTextEdit,
    QHeaderView, QFileDialog, QListWidget, QListWidgetItem)
from . import gameplay, species as sp
from .formats import EditorError


def catalogs(project):
    """Supported named choices, shared with the story trainer form."""
    result = {}
    for kind in (*gameplay.CATALOGS, *gameplay.SPECIES_KINDS):
        data = []; offset = 0
        while True:
            page = project.gameplay_catalog(kind, offset=offset, limit=100)
            data.extend(r for r in page['entries'] if r['supported']); offset += 100
            if offset >= page['total']:
                break
        result[kind] = data
    # Species references for teams, wild slots, gifts and evolution targets: base species
    # plus the ROM-qualified expanded species and regional forms, keyed by packed word.
    refs = [{'id': e['id'], 'name': e['name']} for e in result['species']]
    offset = 0
    while True:
        page = project.gameplay_catalog('species_forms', offset=offset, limit=600)
        refs.extend({'id': r['species'] | r['form'] << 11, 'name': r['name']} for r in page['entries']); offset += 600
        if offset >= page['total']:
            break
    result['species_refs'] = refs
    for kind in ('evolution_methods', 'evolution_items'):
        result[kind] = project.gameplay_catalog(kind, limit=600)['entries']
    return result


def ref(word):
    """Packed word -> request reference (plain number for form 0)."""
    return word if word < 2048 else {'species': word & 0x7ff, 'form': word >> 11}


def named_combo(project, choices, kind, value, changed=None):
    w = QComboBox(); w.setMinimumContentsLength(10)
    w.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    for e in choices[kind]:
        w.addItem(f"{e['name']} · {e['id']}", e['id'])
    index = w.findData(value)
    if index < 0:
        label = (gameplay.entry(project, kind, value)['name'] if kind in gameplay.CATALOGS else
                 gameplay.word_name(project, value) if kind == 'species_refs' and value else f'{kind} {value}')
        w.addItem(f"{label} · {value} (read-only)", value); index = w.count()-1
    w.setCurrentIndex(index)
    if changed:
        w.currentIndexChanged.connect(changed)
    return w


class GameplayEditor(QDialog):
    def __init__(self, project, parent=None, header=None):
        super().__init__(parent)
        self.project = project; self.loading = True; self.pending = None
        self.setWindowTitle('Sovereign Editor · Teams, encounters & species')
        self.resize(1180, 820)
        layout = QVBoxLayout(self)
        top = QHBoxLayout(); layout.addLayout(top)
        self.title = QLabel(); self.title.setStyleSheet('font-size:22px;font-weight:600'); top.addWidget(self.title); top.addStretch()
        top.addWidget(QLabel('Area')); self.area = QComboBox(); self.area.setMinimumContentsLength(26); top.addWidget(self.area)
        self.status = QLabel(); self.status.setWordWrap(True); layout.addWidget(self.status)
        self.area_note = QLabel(); self.area_note.setWordWrap(True); layout.addWidget(self.area_note)
        self.tabs = QTabWidget(); layout.addWidget(self.tabs, 1)
        team = QWidget(); tl = QVBoxLayout(team); self.tabs.addTab(team, 'Trainer team')
        self.trainers = QComboBox(); tl.addWidget(self.trainers)
        self.policy = QLabel(gameplay.POLICY); self.policy.setWordWrap(True); tl.addWidget(self.policy)
        controls = QHBoxLayout(); tl.addLayout(controls)
        self.custom = QCheckBox('Use chosen moves for the whole team'); controls.addWidget(self.custom)
        self.add = QPushButton('Add Pokémon'); self.remove = QPushButton('Remove selected'); controls.addWidget(self.add); controls.addWidget(self.remove); controls.addStretch()
        self.team = QTableWidget(0, 7); self.team.setHorizontalHeaderLabels(['Species','Level','Move 1','Move 2','Move 3','Move 4','Held item'])
        self.team.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch); tl.addWidget(self.team, 1)
        note = QLabel('Existing slot difficulty, ability override and capsule are preserved. New slots use zero defaults. '
                      'Species include the ROM-qualified expanded species and regional forms (Coverage: docs/ASSETS_GAMEPLAY_COVERAGE.md). '
                      'Trainer records are global: every event that uses this trainer sees the change.')
        note.setWordWrap(True); tl.addWidget(note)
        wild = QWidget(); wl = QVBoxLayout(wild); self.tabs.addTab(wild, 'Wild encounters')
        row = QHBoxLayout(); wl.addLayout(row)
        self.method = QComboBox()
        for key in ('grass', *gameplay.METHODS):
            self.method.addItem(key.replace('_',' ').title(), key)
        self.time = QComboBox()
        for t in gameplay.TIMES:
            self.time.addItem(t.title(), t)
        self.rate = QSpinBox(); self.rate.setRange(0,100)
        row.addWidget(QLabel('Method')); row.addWidget(self.method); row.addWidget(QLabel('Time')); row.addWidget(self.time)
        row.addStretch(); row.addWidget(QLabel('Stored rate')); row.addWidget(self.rate)
        self.wild_note = QLabel(); self.wild_note.setWordWrap(True); wl.addWidget(self.wild_note)
        self.wild = QTableWidget(); self.wild.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch); wl.addWidget(self.wild, 1)
        wl.addWidget(QLabel('Other methods, radio and swarm replacements stay unchanged unless explicitly edited.'))
        self.tabs.addTab(self.species_panel(), 'Species')
        from .data_ui import RecordsPanel
        self.records = RecordsPanel(project, on_applied=lambda: self.guard(self.reset)); self.tabs.addTab(self.records, 'Records')
        from .pokemon_ui import PokemonPanel
        self.pokemon = PokemonPanel(project, on_applied=lambda: self.guard(self.reset)); self.tabs.addTab(self.pokemon, 'Pokémon packages')
        self.preview = QPlainTextEdit(); self.preview.setReadOnly(True); self.preview.setMaximumHeight(190); layout.addWidget(self.preview)
        buttons = QHBoxLayout(); layout.addLayout(buttons)
        for attr, label, callback in [('preview_button','Preview changes',self.preview_changes), ('apply_button','Apply',self.apply),
                                     ('discard_button','Discard / reload',self.reload),('undo_button','Undo',self.undo),
                                     ('redo_button','Redo',self.redo),('export_button','Export…',self.export)]:
            b = QPushButton(label); setattr(self,attr,b); b.clicked.connect(lambda checked=False, cb=callback:self.guard(cb)); buttons.addWidget(b)
        close = QPushButton('Close'); close.clicked.connect(self.close); buttons.addWidget(close)
        self.catalogs = catalogs(project)
        self.areas = []
        offset = 0
        while True:
            page = project.gameplay_areas(offset=offset, limit=400)
            self.areas.extend(a for a in page['areas'] if a['encounters'] or a['trainers']); offset += 400
            if offset >= page['total']:
                break
        for a in self.areas:
            self.area.addItem(f"{a['name']} · header {a['header']}", a['header'])
        self.header = header if any(a['header'] == header for a in self.areas) else 34
        self.area.setCurrentIndex(max(0, self.area.findData(self.header)))
        self.trainers.currentIndexChanged.connect(self.select_trainer)
        self.custom.toggled.connect(self.custom_changed)
        self.add.clicked.connect(self.add_member); self.remove.clicked.connect(self.remove_member)
        self.method.currentIndexChanged.connect(self.show_wild); self.time.currentIndexChanged.connect(self.show_wild)
        self.rate.valueChanged.connect(self.rate_changed)
        self.area.currentIndexChanged.connect(lambda *_: self.guard(self.select_area))
        self.species.currentIndexChanged.connect(lambda *_: self.guard(self.select_species))
        self.draft_teams = {}; self.draft_wild = {}; self.draft_species = {}
        self.reset()

    # ---- species panel -------------------------------------------------------

    def species_panel(self):
        w = QWidget(); l = QVBoxLayout(w)
        row = QHBoxLayout(); l.addLayout(row); row.addWidget(QLabel('Species')); self.species = QComboBox(); row.addWidget(self.species, 1)
        self.species_note = QLabel(); self.species_note.setWordWrap(True); l.addWidget(self.species_note)
        grid = QGridLayout(); l.addLayout(grid); self.stats = {}
        for i, key in enumerate(sp.STATS):
            box = QSpinBox(); box.setRange(1, 255); box.valueChanged.connect(self.invalidate); self.stats[key] = box
            grid.addWidget(QLabel({'hp': 'HP'}.get(key, key.replace('_', ' ').title())), 0, i); grid.addWidget(box, 1, i)
        self.type_row = QHBoxLayout(); self.ability_row = QHBoxLayout(); l.addLayout(self.type_row); l.addLayout(self.ability_row)
        self.types = []; self.abilities = []; self.growth = None
        split = QHBoxLayout(); l.addLayout(split, 1)
        left = QVBoxLayout(); split.addLayout(left, 3)
        left.addWidget(QLabel('Level-up moves (level 0 = learned on evolution). Unlisted entries are never normalized.'))
        self.learnset = QTableWidget(0, 2); self.learnset.setHorizontalHeaderLabels(['Level', 'Move'])
        self.learnset.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch); left.addWidget(self.learnset, 1)
        lr = QHBoxLayout(); left.addLayout(lr)
        self.add_move = QPushButton('Add move'); self.remove_move = QPushButton('Remove selected move'); lr.addWidget(self.add_move); lr.addWidget(self.remove_move); lr.addStretch()
        self.add_move.clicked.connect(lambda: self.append_learn({'level': 1, 'move': 33}, True))
        self.remove_move.clicked.connect(self.remove_learn)
        left.addWidget(QLabel('Evolution slots: level, item, friendship, held item, known move/type and day/night methods. '
                              'Other methods are preserved read-only. First qualifying slot wins.'))
        self.evolutions = QTableWidget(0, 3); self.evolutions.setHorizontalHeaderLabels(['Method', 'Parameter', 'Target'])
        self.evolutions.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch); left.addWidget(self.evolutions, 1)
        right = QVBoxLayout(); split.addLayout(right, 2)
        right.addWidget(QLabel('TM/HM compatibility (base TM001–TM092, HM01–HM08)'))
        self.machines = QListWidget(); right.addWidget(self.machines, 1)
        self.machines.itemChanged.connect(self.invalidate)
        return w

    def setup_species_choices(self):
        self.species.blockSignals(True); self.species.clear()
        for e in self.catalogs['species_refs']:
            self.species.addItem(f"{e['name']} · {e['id'] & 0x7ff}" + (f" form {e['id'] >> 11}" if e['id'] >> 11 else ''), e['id'])
        self.species.setCurrentIndex(max(0, self.species.findData(161))); self.species.blockSignals(False)
        for layout, attr, label, kind in ((self.type_row, 'types', 'Type', 'types'), (self.ability_row, 'abilities', 'Ability', 'abilities')):
            for i in range(2):
                combo = QComboBox(); combo.currentIndexChanged.connect(self.invalidate)
                if kind == 'abilities' and i == 1:
                    combo.addItem('None · 0', 0)
                for e in self.catalogs[kind]:
                    combo.addItem(f"{e['name']} · {e['id']}", e['id'])
                layout.addWidget(QLabel(f'{label} {i+1}')); layout.addWidget(combo, 1); getattr(self, attr).append(combo)
        self.growth = QComboBox(); self.growth.currentIndexChanged.connect(self.invalidate)
        for e in self.catalogs['growth']:
            self.growth.addItem(e['name'], e['id'])
        self.ability_row.addWidget(QLabel('Growth')); self.ability_row.addWidget(self.growth, 1)
        for m in self.catalogs['machines']:
            item = QListWidgetItem(f"{m['name']} · {gameplay.entry(self.project, 'moves', m['move'])['name']}")
            item.setData(Qt.ItemDataRole.UserRole, m['id']); item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked); self.machines.addItem(item)

    def append_learn(self, row, user=False):
        r = self.learnset.rowCount(); self.learnset.insertRow(r)
        level = QSpinBox(); level.setRange(0, 100); level.setValue(row['level']); level.valueChanged.connect(self.invalidate)
        self.learnset.setCellWidget(r, 0, level); self.learnset.setCellWidget(r, 1, named_combo(self.project, self.catalogs, 'moves', row['move'], self.invalidate))
        if user:
            self.invalidate()

    def remove_learn(self):
        if self.learnset.currentRow() >= 0:
            self.learnset.removeRow(self.learnset.currentRow()); self.invalidate()

    def select_species(self):
        if self.loading:
            return
        self.store_species()
        self.loading = True
        sid = self.species.currentData()
        view = self.draft_species.get(sid, {}).get('view') or self.project.gameplay_species(sid & 0x7ff, sid >> 11)
        form = self.draft_species.get(sid, {}).get('form') or self.species_form(view)
        self.species_view = view
        for key, box in self.stats.items():
            box.setValue(form['stats'][key])
        for combo, value in zip(self.types + self.abilities, form['types'] + form['abilities']):
            index = combo.findData(value)
            if index < 0:
                combo.addItem(f'{value} (unlisted · kept unless changed)', value); index = combo.count()-1
            combo.setCurrentIndex(index)
        self.growth.setCurrentIndex(max(0, self.growth.findData(form['growth'])))
        self.learnset.setRowCount(0)
        for row in form['learnset']:
            self.append_learn(row)
        self.evolutions.setRowCount(0)
        for e in form['evolutions']:
            self.evolution_row(e)
        for i in range(self.machines.count()):
            item = self.machines.item(i)
            item.setCheckState(Qt.CheckState.Checked if item.data(Qt.ItemDataRole.UserRole) in form['machines'] else Qt.CheckState.Unchecked)
        editable = all(view['editable'].values())
        self.species_note.setText('Species records are global: all trainers, encounters and evolutions using this species change. '
                                  'Stored party Pokémon keep saved moves, ability number and experience; inspect edits on fresh Pokémon. '
                                  'Ability slots select an existing ability; they do not implement its effect.'
                                  + ('' if editable else ' Read-only parts: ' + '; '.join(view['reasons'].values())))
        self.loading = False

    def evolution_row(self, e):
        r = self.evolutions.rowCount(); self.evolutions.insertRow(r)
        method = QComboBox(); method.addItem('Empty', None)
        for m in self.catalogs['evolution_methods']:
            method.addItem(m['name'].replace('_', ' '), m['name'])
        if e['method_id'] and not e['editable']:
            method.addItem(f"{e['method']} (preserved)", e['method'])
        method.setCurrentIndex(max(0, method.findData(e['method'])))
        target = QComboBox(); target.addItem('No evolution', 0)
        for s_ in self.catalogs['species_refs']:
            target.addItem(s_['name'], s_['id'])
        word = e['target'] | e['form'] << 11 if e['method_id'] else 0
        index = target.findData(word)
        if index < 0:
            target.addItem(f"{gameplay.word_name(self.project, word)} (read-only)", word); index = target.count() - 1
        target.setCurrentIndex(index)
        self.evolutions.setCellWidget(r, 0, method); self.evolutions.setCellWidget(r, 2, target)
        self.evolution_param(r, e['param'])
        method.currentIndexChanged.connect(lambda *_, r=r: (self.evolution_param(r, 0), self.invalidate()))
        target.currentIndexChanged.connect(self.invalidate)
        for c in (0, 1, 2):
            self.evolutions.cellWidget(r, c).setEnabled(e['editable'])

    def evolution_param(self, r, value):
        """Typed parameter widget for the row's method (level, item, move, type or none)."""
        name = self.evolutions.cellWidget(r, 0).currentData()
        kind = sp.EVOLUTION_METHODS.get(name, (0, 'none'))[1] if name else 'none'
        if kind == 'level':
            w = QSpinBox(); w.setRange(1, 100); w.setValue(value if 1 <= value <= 100 else 1); w.valueChanged.connect(self.invalidate)
        elif kind in ('use_item', 'held_item', 'move', 'type'):
            catalog = {'use_item': 'evolution_items', 'held_item': 'items', 'move': 'moves', 'type': 'types'}[kind]
            w = self.combo(catalog, value)
        else:
            w = QLabel('—')
        w.setProperty('kind', kind); self.evolutions.setCellWidget(r, 1, w)

    def evolution_value(self, r, e):
        name = self.evolutions.cellWidget(r, 0).currentData(); target = self.evolutions.cellWidget(r, 2).currentData()
        widget = self.evolutions.cellWidget(r, 1); kind = widget.property('kind')
        param = widget.value() if kind == 'level' else widget.currentData() if kind != 'none' else 0
        if not name or not target:
            return {**e, 'method': None, 'method_id': 0, 'param': 0, 'target': 0, 'form': 0}
        return {**e, 'method': name, 'method_id': sp.EVOLUTION_METHODS[name][0], 'param': param,
                'target': target & 0x7ff, 'form': target >> 11}

    @staticmethod
    def species_form(view):
        return {'stats': dict(view['personal']['stats']), 'types': list(view['personal']['types']),
                'abilities': list(view['personal']['abilities']), 'growth': view['personal']['growth'],
                'learnset': copy.deepcopy(view['learnset']),
                'evolutions': copy.deepcopy(view['evolution_v3']), 'machines': list(view['machines']['base_compatible'])}

    def species_value(self):
        view = self.species_view
        evolutions = copy.deepcopy(view['evolution_v3'])
        for r, e in enumerate(evolutions):
            if e['editable']:
                evolutions[r] = self.evolution_value(r, e)
        return {'stats': {k: b.value() for k, b in self.stats.items()}, 'types': [c.currentData() for c in self.types],
                'abilities': [c.currentData() for c in self.abilities], 'growth': self.growth.currentData(),
                'learnset': [{'level': self.learnset.cellWidget(r, 0).value(), 'move': self.learnset.cellWidget(r, 1).currentData()}
                             for r in range(self.learnset.rowCount())],
                'evolutions': evolutions,
                'machines': sorted(self.machines.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.machines.count())
                                   if self.machines.item(i).checkState() == Qt.CheckState.Checked)}

    def store_species(self):
        view = getattr(self, 'species_view', None)
        if view is not None and all(view['editable'].values()):
            self.draft_species[view['species'] | view.get('form', 0) << 11] = {'view': view, 'form': self.species_value()}

    @staticmethod
    def species_changes(view, form):
        before = GameplayEditor.species_form(view); changes = {}
        stats = {k: v for k, v in form['stats'].items() if v != before['stats'][k]}
        if stats: changes['stats'] = stats
        for key in ('types', 'abilities', 'growth'):
            if form[key] != before[key]: changes[key] = form[key]
        old, new = Counter(map(lambda r: (r['level'], r['move']), before['learnset'])), Counter(map(lambda r: (r['level'], r['move']), form['learnset']))
        remove = [{'level': l, 'move': m} for (l, m), n in (old - new).items() for _ in range(n)]
        add = [{'level': l, 'move': m} for (l, m), n in (new - old).items() for _ in range(n)]
        if remove or add:
            changes['learnset'] = {k: v for k, v in (('remove', remove), ('add', add)) if v}
        evo = []
        for a, b in zip(before['evolutions'], form['evolutions']):
            if a['editable'] and (a['method_id'], a['param'], a['target'], a['form']) != (b['method_id'], b['param'], b['target'], b['form']):
                target = {'species': b['target']} | ({'form': b['form']} if b['form'] else {})
                evo.append({'slot': a['slot'], 'clear': True} if not b['method_id'] else
                           {'slot': a['slot'], 'method': b['method'], 'param': b['param'], 'target': target}
                           | ({} if sp.EVOLUTION_METHODS[b['method']][1] != 'none' else {'param': 0}))
        if evo: changes['evolutions'] = evo
        machines = [{'machine': i, 'compatible': i in form['machines']} for i in sp.BASE_MACHINES
                    if (i in form['machines']) != (i in before['machines'])]
        if machines: changes['machines'] = machines
        return changes

    # ---- areas, teams and encounters ----------------------------------------

    def guard(self, callback):
        try:
            return callback()
        except (EditorError, OSError, ValueError, TypeError, KeyError) as exc:
            self.status.setText(str(exc)); self.pending = None; self.apply_button.setEnabled(False)

    def invalidate(self, *args):
        if not self.loading:
            self.pending = None; self.apply_button.setEnabled(False)
            self.status.setText(f'Revision {self.revision} · Unsaved form changes. Preview before applying.')

    def combo(self, kind, value):
        return named_combo(self.project, self.catalogs, kind, value, self.invalidate)

    def spin(self, value, low=1):
        w = QSpinBox(); w.setRange(low,100); w.setValue(value); w.valueChanged.connect(self.invalidate); return w

    def reset(self):
        self.loading = True
        if not self.species.count():
            self.setup_species_choices()
        self.draft_teams = {}; self.draft_wild = {}; self.draft_species = {}; self.trainer = None; self.species_view = None
        self.pending = None; self.loading = False
        self.load_area(); self.select_species()
        self.apply_button.setEnabled(False); self.preview.clear()
        self.status.setText(f'Revision {self.revision} · Preview stages a single undoable edit.')
        self.undo_button.setEnabled(bool(self.project.doc['history'])); self.redo_button.setEnabled(bool(self.project.doc.get('redo')))

    def select_area(self):
        if self.loading:
            return
        self.store_area(); self.header = self.area.currentData(); self.load_area()

    def store_area(self):
        if self.trainer and self.trainer['editable']:
            self.draft_teams[self.trainer['id']] = (self.header, self.team_value())

    def load_area(self):
        self.loading = True; self.header = self.area.currentData()
        self.data = self.project.gameplay_data(self.header); self.revision = self.data['revision']
        self.title.setText(f"{self.data['route']} · Teams, encounters & species")
        enc = self.data['encounters']
        if enc is not None and self.header not in self.draft_wild:
            self.draft_wild[self.header] = copy.deepcopy(enc['methods'])
        shared = (enc or {}).get('shared_with') or []
        self.area_note.setText(f"Header {self.header} · event file {self.data['resources']['event_member']} · "
                               + (f"encounter file {enc['member']}" + (f" (shared with headers {shared})" if shared else ' (private)')
                                  if enc else self.data.get('encounter_reason', 'No encounters')))
        self.tabs.setTabEnabled(1, enc is not None)
        selected = self.trainers.currentData(); self.trainers.clear(); self.trainer = None
        for t in self.data['trainers']:
            self.trainers.addItem(f"{t['name']} · Trainer {t['id']}" + ('' if t['editable'] else ' · read-only'), t['id'])
        self.tabs.setTabEnabled(0, bool(self.data['trainers']))
        self.trainers.setCurrentIndex(max(0, self.trainers.findData(selected or 47)))
        self.loading = False; self.select_trainer(); self.show_wild()

    def select_trainer(self, *args):
        if self.loading:
            return
        if self.trainer and self.trainer['editable']:
            self.draft_teams[self.trainer['id']] = (self.header, self.team_value())
        self.loading = True
        self.trainer = next((t for t in self.data['trainers'] if t['id'] == self.trainers.currentData()), None)
        self.team.setRowCount(0)
        if self.trainer is None:
            self.loading = False; return
        rows = self.draft_teams.get(self.trainer['id'], (None, self.trainer.get('party', [])))[1]
        self.custom.setChecked(bool(rows and rows[0]['moves'] is not None))
        for m in rows:
            self.append_row(m)
        editable = self.trainer['editable']
        for w in (self.team,self.custom,self.add,self.remove):
            w.setEnabled(editable)
        self.policy.setText(gameplay.POLICY if editable else self.trainer['reason'])
        self.loading = False; self.invalidate()

    def append_row(self, m):
        r = self.team.rowCount(); self.team.insertRow(r)
        self.team.setCellWidget(r,0,self.combo('species_refs',m['species'] | m.get('form',0) << 11)); self.team.setCellWidget(r,1,self.spin(m['level']))
        for i, move in enumerate(m['moves'] or [0]*4):
            w = self.combo('moves',move); w.setEnabled(self.custom.isChecked()); self.team.setCellWidget(r,2+i,w)
        self.team.setCellWidget(r,6,self.combo('items',m['held_item']))

    def custom_changed(self, *args):
        for r in range(self.team.rowCount()):
            for c in range(2,6):
                self.team.cellWidget(r,c).setEnabled(self.custom.isChecked())
        self.invalidate()

    def add_member(self):
        if self.team.rowCount() < 6:
            self.append_row({'species':16,'level':5,'moves':None,'held_item':0}); self.invalidate()

    def remove_member(self):
        if self.team.rowCount() > 1 and self.team.currentRow() >= 0:
            self.team.removeRow(self.team.currentRow()); self.invalidate()

    def team_value(self):
        return [{'species':self.team.cellWidget(r,0).currentData() & 0x7ff, 'form':self.team.cellWidget(r,0).currentData() >> 11,
                 'level':self.team.cellWidget(r,1).value(),
                 'moves':[self.team.cellWidget(r,c).currentData() for c in range(2,6)] if self.custom.isChecked() else None,
                 'held_item':self.team.cellWidget(r,6).currentData()} for r in range(self.team.rowCount())]

    def show_wild(self, *args):
        if self.loading or self.header not in self.draft_wild:
            return
        self.loading = True; method = self.method.currentData(); time = self.time.currentData()
        view = self.draft_wild[self.header][method]; grass = method == 'grass'; self.time.setEnabled(grass); self.rate.setValue(view['rate'])
        self.wild.clear(); self.wild.setColumnCount(3 if grass else 4)
        self.wild.setHorizontalHeaderLabels(['Slot','Level · all times','Species'] if grass else ['Slot','Minimum level','Maximum level','Species'])
        self.wild.setRowCount(12 if grass else len(view['slots']))
        self.wild_note.setText('Grass levels apply to morning, day and night. Species uses the selected time. Stored rate is not a per-step probability.' if grass
                              else 'Each slot stores a level range. A rate of zero disables this method. Slot probabilities are engine-controlled.')
        for i in range(self.wild.rowCount()):
            self.wild.setCellWidget(i,0,QLabel(str(i+1)))
            if grass:
                w = self.spin(view['levels'][i]); w.valueChanged.connect(lambda v, i=i: self.wild_set('grass','levels',i,v)); self.wild.setCellWidget(i,1,w)
                w = self.combo('species_refs',view[time][i]); w.currentIndexChanged.connect(lambda _, w=w, i=i, t=time:self.wild_set('grass',t,i,w.currentData())); self.wild.setCellWidget(i,2,w)
            else:
                slot = view['slots'][i]
                for c,k in [(1,'min_level'),(2,'max_level')]:
                    w = self.spin(slot[k],0); w.valueChanged.connect(lambda v, m=method,k=k,i=i:self.wild_set(m,k,i,v)); self.wild.setCellWidget(i,c,w)
                w = self.combo('species_refs',slot['species']); w.currentIndexChanged.connect(lambda _,w=w,m=method,i=i:self.wild_set(m,'species',i,w.currentData())); self.wild.setCellWidget(i,3,w)
        self.loading = False

    def wild_set(self, method, field, slot, value):
        draft = self.draft_wild[self.header]
        if method == 'grass':
            draft[method][field][slot] = value
        else:
            draft[method]['slots'][slot][field] = value
        self.invalidate()

    def rate_changed(self, v):
        if not self.loading and self.header in self.draft_wild:
            self.draft_wild[self.header][self.method.currentData()]['rate'] = v; self.invalidate()

    def encounter_edits(self, before_all, after_all):
        edits = []
        for method, after in after_all.items():
            before = before_all[method]
            if before['rate'] != after['rate']:
                edits.append({'method':method,'field':'rate','value':after['rate']})
            if method == 'grass':
                for k in ('levels', *gameplay.TIMES):
                    for i, v in enumerate(after[k]):
                        if v != before[k][i]:
                            e = {'method':method,'field':'level' if k == 'levels' else 'species','slot':i,
                                 'value':v if k == 'levels' else ref(v)}
                            if k != 'levels':
                                e['time'] = k
                            edits.append(e)
            else:
                for i, s in enumerate(after['slots']):
                    old = before['slots'][i]
                    if s['species'] != old['species']:
                        edits.append({'method':method,'field':'species','slot':i,'value':ref(s['species'])})
                    if [s['min_level'],s['max_level']] != [old['min_level'],old['max_level']]:
                        edits.append({'method':method,'field':'levels','slot':i,'value':[s['min_level'],s['max_level']]})
        return edits

    def request(self):
        operations = []
        self.store_area(); self.store_species()
        for tid, (header, value) in sorted(self.draft_teams.items()):
            trainer = next(t for t in self.project.gameplay_data(header)['trainers'] if t['id'] == tid)
            if value != gameplay.team_value_v3(trainer['party']):
                operations.append({'kind':'trainer','header':header,'id':tid,'before_sha256':trainer['before_sha256'],'party':value})
        for header, after in sorted(self.draft_wild.items()):
            enc = self.project.gameplay_data(header)['encounters']
            edits = self.encounter_edits(enc['methods'], after)
            if edits:
                operations.append({'kind':'encounters','header':header,'before_sha256':enc['before_sha256'],'edits':edits})
        for sid, draft in sorted(self.draft_species.items()):
            changes = self.species_changes(draft['view'], draft['form'])
            if changes:
                operations.append({'kind':'species','id':sid & 0x7ff,'before_sha256':draft['view']['before_sha256'],'changes':changes}
                                  | ({'form': sid >> 11} if sid >> 11 else {}))
        return {'header':self.header,'operations':operations}

    def describe(self, plan):
        lines = []
        def named(kind, value):
            return gameplay.entry(self.project, kind, value)['name']
        def mon(m):
            moves = 'level-up moves' if m['moves'] is None else ', '.join(named('moves', v) for v in m['moves'] if v)
            who = gameplay.word_name(self.project, m['species'] | m.get('form', 0) << 11)
            return f"{who} Lv{m['level']} · {moves} · {named('items',m['held_item'])}"
        for p in plan['preview']:
            if p['kind'] == 'trainer':
                lines.append(f"{p['name']} · Trainer {p['id']} (header {p['header']}) · {len(p['before'])} → {len(p['after'])} Pokémon")
                for i,m in enumerate(p['after']):
                    old = mon(p['before'][i]) if i < len(p['before']) else 'new slot'
                    lines.append(f"  Slot {i+1}: {old}\n       → {mon(m)}")
                for i in range(len(p['after']), len(p['before'])):
                    lines.append(f"  Removed slot {i+1}: {mon(p['before'][i])}")
            elif p['kind'] == 'encounters':
                shared = f" · shared with {p['shared_with']}" if p['shared_with'] else ''
                lines.append(f"Encounters · header {p['header']} · file {p['member']}{shared}")
                for e in p['edits']:
                    method, field = e['method'],e['field']; b = p['before'][method]
                    if field == 'rate': old = b['rate']
                    elif method == 'grass': old = b['levels' if field == 'level' else e['time']][e['slot']]
                    else:
                        s=b['slots'][e['slot']]; old=s['species'] if field == 'species' else [s['min_level'],s['max_level']]
                    new = e['value']
                    if field == 'species':
                        word = new if type(new) is int else new['species'] | new.get('form', 0) << 11
                        old, new = gameplay.word_name(self.project, old), gameplay.word_name(self.project, word)
                    lines.append(f"  {method} {e.get('time','')} slot {e['slot']+1 if 'slot' in e else 'all'} {field}: {old} → {new}")
            else:
                b, a = p['before'], p['after']
                lines.append(f"{p['name']} · species {p['id']}" + (f" form {p['form']} (row {p['personal_index']})" if p.get('form') else ''))
                for key in ('stats', 'types', 'abilities', 'growth'):
                    if b['personal'][key] != a['personal'][key]:
                        lines.append(f"  {key}: {b['personal'][key]} → {a['personal'][key]}")
                if b['learnset'] != a['learnset']:
                    lines.append('  learnset: ' + ', '.join(f"{r['level']}:{named('moves', r['move'])}" for r in a['learnset']
                                                        if r not in b['learnset']) + ' added'
                                 + ''.join(f"; removed {r['level']}:{r['move']}" for r in b['learnset'] if r not in a['learnset']))
                if b['evolutions'] != a['evolutions']:
                    def evo(e):
                        return f"{e['method']}({e['param']})→{gameplay.word_name(self.project, e['target'] | e['form'] << 11)}"
                    lines.append(f"  evolutions: {[evo(e) for e in b['evolutions']]} → {[evo(e) for e in a['evolutions']]}")
                if b['machines'] != a['machines']:
                    added = sorted(set(a['machines']) - set(b['machines'])); removed = sorted(set(b['machines']) - set(a['machines']))
                    name = {m['id']: m['name'] for m in self.catalogs['machines']}
                    lines.append(f"  TM/HM: +{[name[i] for i in added]} −{[name[i] for i in removed]}")
                lines.extend(f'  advisory: {n}' for n in p['advisories'])
        impact = plan['transaction'].get('impact')
        if impact:
            lines.append(f"Impact · {len(impact['trainers'])} stock trainers, {len(impact['authored_trainers'])} authored trainers, "
                         f"{len(impact['encounter_files'])} encounter files, {len(impact['evolution_sources'])} evolution sources use edited species.")
            lines.extend(f'  {n}' for n in impact['notes'])
        return '\n'.join(lines)

    def preview_changes(self):
        request = self.request()
        if not request['operations']:
            self.preview.setPlainText('No changes.'); self.pending = None; self.apply_button.setEnabled(False); return
        plan = self.project.plan_gameplay_edit(**request)
        self.preview.setPlainText(self.describe(plan)); self.pending = request
        self.apply_button.setEnabled(not plan['empty']); self.status.setText('Preview ready · Apply saves this batch as one undoable edit.')

    def apply(self):
        if self.pending:
            self.project.apply_gameplay_edit(self.revision, **self.pending); self.reset()

    def reload(self):
        self.project._read(); self.reset()

    def undo(self):
        self.project.undo(self.revision); self.reset()

    def redo(self):
        self.project.redo(self.revision); self.reset()

    def export(self):
        if self.request()['operations']:
            raise ValueError('Apply or discard the form changes before exporting.')
        path, _ = QFileDialog.getSaveFileName(self, 'New export folder (must not exist)')
        if not path:
            return
        save, _ = QFileDialog.getOpenFileName(self, 'Optional ordinary save to copy (Cancel skips)', filter='Save (*.sav)')
        self.project.export(path, self.revision, save or None)
        self.status.setText(f'Exported {path} · Native acceptance pending.')
