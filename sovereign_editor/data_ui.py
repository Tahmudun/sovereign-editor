"""Records tab: move records, item prices/use parameters, TM mappings and qualified catalogs.

Backed only by Project.plan_data_edit / apply_data_edit (the same operations as the CLI
data-edit command). Every staged change shows its before/after and shared users.
"""
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QPushButton, QComboBox,
                               QSpinBox, QLineEdit, QListWidget, QPlainTextEdit, QGroupBox)

from . import game_data as gd
from .formats import EditorError


def spin(low, high, value=0):
    w = QSpinBox(); w.setRange(low, high); w.setValue(value); return w


class RecordsPanel(QWidget):
    def __init__(self, project, on_applied=None):
        super().__init__()
        self.project, self.on_applied, self.operations = project, on_applied, []
        layout = QVBoxLayout(self)
        note = QLabel('Records are global: every Pokémon, trainer, tutor and mart using a move, item or TM sees the change. '
                      'Only existing records and effects are offered; new move, item, effect or ability code is out of scope.')
        note.setWordWrap(True); layout.addWidget(note)
        row = QHBoxLayout(); layout.addLayout(row)
        row.addWidget(self.move_group()); row.addWidget(self.item_group()); row.addWidget(self.machine_group())
        row2 = QHBoxLayout(); layout.addLayout(row2, 1)
        row2.addWidget(self.shop_group()); row2.addWidget(self.catalog_group(), 1)
        self.queue = QListWidget(); self.queue.setMaximumHeight(80); layout.addWidget(self.queue)
        self.preview = QPlainTextEdit(); self.preview.setReadOnly(True); self.preview.setMaximumHeight(170); layout.addWidget(self.preview)
        buttons = QHBoxLayout(); layout.addLayout(buttons)
        for label, callback in (('Preview records', self.preview_records), ('Apply records', self.apply),
                                ('Discard staged records', self.discard)):
            b = QPushButton(label); b.clicked.connect(lambda _=False, cb=callback: self.guard(cb)); buttons.addWidget(b)
        self.status = QLabel(); self.status.setWordWrap(True); layout.addWidget(self.status)
        self.reload()

    def guard(self, fn):
        try:
            fn()
        except (EditorError, ValueError, KeyError) as exc:
            self.status.setText(str(exc))

    # ---- groups ------------------------------------------------------------------------------

    def move_group(self):
        box = QGroupBox('Move record'); f = QFormLayout(box)
        self.move = QComboBox(); self.move.currentIndexChanged.connect(lambda *_: self.guard(self.load_move)); f.addRow('Move', self.move)
        self.power, self.accuracy, self.pp = spin(0, 255), spin(0, 100), spin(1, 40)
        self.move_type, self.category, self.effect = QComboBox(), QComboBox(), QComboBox()
        for t in gd.TYPES:
            self.move_type.addItem(gd._name(self.project, gd.TYPE_BANK, t), t)
        for i, c in enumerate(gd.CATEGORIES):
            self.category.addItem(c.capitalize(), i)
        self.priority, self.chance = spin(-7, 7), spin(0, 100)
        for label, w in (('Power', self.power), ('Accuracy', self.accuracy), ('PP', self.pp), ('Type', self.move_type),
                         ('Category', self.category), ('Priority', self.priority), ('Battle effect', self.effect),
                         ('Effect chance %', self.chance)):
            f.addRow(label, w)
        b = QPushButton('Stage move change'); b.clicked.connect(lambda: self.guard(self.stage_move)); f.addRow(b)
        return box

    def item_group(self):
        box = QGroupBox('Item price and use'); f = QFormLayout(box)
        self.item = QComboBox(); self.item.currentIndexChanged.connect(lambda *_: self.guard(self.load_item)); f.addRow('Item', self.item)
        self.price, self.hp, self.pp_restore = spin(0, 65535), spin(1, 255), spin(1, 255)
        self.evs, self.friendship = QLineEdit(), QLineEdit()
        self.evs.setPlaceholderText('HP/Atk/Def/Spe/SpA/SpD'); self.friendship.setPlaceholderText('low/medium/high band')
        self.item_rows = {'price': self.price, 'hp_restore': self.hp, 'pp_restore': self.pp_restore,
                          'ev_gain': self.evs, 'friendship': self.friendship}
        for key, w in self.item_rows.items():
            f.addRow(gd.ITEM_FAMILIES[key].split(' (')[0], w)
        self.item_form = f
        b = QPushButton('Stage item change'); b.clicked.connect(lambda: self.guard(self.stage_item)); f.addRow(b)
        return box

    def machine_group(self):
        box = QGroupBox('TM mapping'); f = QFormLayout(box)
        self.machine = QComboBox(); self.machine.currentIndexChanged.connect(lambda *_: self.guard(self.load_machine)); f.addRow('Machine', self.machine)
        self.machine_move = QComboBox(); f.addRow('Teaches', self.machine_move)
        self.machine_note = QLabel('Compatibility is per TM number: every compatible species can learn the new move. '
                                   'HM mappings stay stock (field authority is keyed to the move).'); self.machine_note.setWordWrap(True); f.addRow(self.machine_note)
        b = QPushButton('Stage TM change'); b.clicked.connect(lambda: self.guard(self.stage_machine)); f.addRow(b)
        return box

    def shop_group(self):
        """Named mart inventories (field_services.py): clerks sell them through the native special mart."""
        box = QGroupBox('Shop inventory'); f = QFormLayout(box)
        self.shop = QComboBox(); self.shop.currentIndexChanged.connect(lambda *_: self.guard(self.load_shop)); f.addRow('Shop', self.shop)
        self.shop_name = QLineEdit(); self.shop_name.setPlaceholderText('stable_name'); f.addRow('Name', self.shop_name)
        self.shop_items = QListWidget(); self.shop_items.setMaximumHeight(110); f.addRow('Sells', self.shop_items)
        self.shop_pick = QComboBox(); f.addRow('Item', self.shop_pick)
        buttons = QHBoxLayout()
        for label, callback in (('Add', self.add_shop_item), ('Remove', self.remove_shop_item)):
            b = QPushButton(label); b.clicked.connect(lambda _=False, cb=callback: self.guard(cb)); buttons.addWidget(b)
        f.addRow(buttons)
        self.shop_note = QLabel(); self.shop_note.setWordWrap(True); f.addRow(self.shop_note)
        buttons = QHBoxLayout()
        for label, callback in (('Stage shop', self.stage_shop), ('Stage removal', self.stage_shop_removal)):
            b = QPushButton(label); b.clicked.connect(lambda _=False, cb=callback: self.guard(cb)); buttons.addWidget(b)
        f.addRow(buttons)
        return box

    def catalog_group(self):
        box = QGroupBox('Qualified catalogs (expanded entries and refusal reasons)'); l = QVBoxLayout(box)
        row = QHBoxLayout(); l.addLayout(row)
        self.catalog_kind = QComboBox(); self.catalog_kind.addItems(['moves', 'items', 'abilities', 'effects', 'machines', 'shops', 'spawns'])
        self.catalog_search = QLineEdit(); self.catalog_search.setPlaceholderText('Search name or ID')
        self.catalog_only = QComboBox(); self.catalog_only.addItems(['All entries', 'Refused only', 'Expanded only'])
        for w in (self.catalog_kind, self.catalog_search, self.catalog_only):
            row.addWidget(w)
        self.catalog_kind.currentIndexChanged.connect(self.show_catalog); self.catalog_search.textChanged.connect(self.show_catalog)
        self.catalog_only.currentIndexChanged.connect(self.show_catalog)
        self.catalog_list = QListWidget(); l.addWidget(self.catalog_list)
        return box

    # ---- loading ------------------------------------------------------------------------------

    def reload(self):
        self.revision = self.project.doc['revision']
        state = self.project.composed()
        self.move.blockSignals(True); self.item.blockSignals(True); self.machine.blockSignals(True); self.machine_move.clear(); self.effect.clear()
        self.move.clear(); self.item.clear(); self.machine.clear()
        for e in gd.catalog(self.project, 'moves', limit=600, state=state)['entries'] + \
                gd.catalog(self.project, 'moves', offset=600, limit=600, state=state)['entries']:
            if e['supported']:
                label = f"{e['name']} ({e['id']}){' · expanded' if e['expanded'] else ''}"
                self.move.addItem(label, e['id']); self.machine_move.addItem(label, e['id'])
        for e in gd.effects(self.project, state):
            self.effect.addItem(e['name'], e['id'])
        for start in range(0, 2700, 600):
            for e in gd.catalog(self.project, 'items', offset=start, limit=600, state=state)['entries']:
                if e['supported']:
                    self.item.addItem(f"{e['name']} ({e['id']})", e['id'])
        for m in gd.machines(self.project, state):
            if m['supported']:
                self.machine.addItem(m['name'], m['id'])
        from . import field_services as fs
        self.shop.blockSignals(True); self.shop.clear(); self.shop_pick.clear(); self.shop.addItem('New shop', None)
        for r in gd.shop_rows(self.project, state):
            self.shop.addItem(f"{r['name']} (mart {r['id']})", r['name'])
        for i in range(self.item.count()):
            if not fs.item_issue(self.project, state, self.item.itemData(i)):
                self.shop_pick.addItem(self.item.itemText(i), self.item.itemData(i))
        for w in (self.move, self.item, self.machine, self.shop):
            w.blockSignals(False)
        self.load_move(); self.load_item(); self.load_machine(); self.load_shop(); self.show_catalog()

    def load_move(self):
        if self.move.currentData() is None:
            return
        r = self.project.data_record('move', self.move.currentData())['record']
        for w, key in ((self.power, 'power'), (self.accuracy, 'accuracy'), (self.pp, 'pp'), (self.priority, 'priority'),
                       (self.chance, 'effect_chance')):
            w.setValue(r[key])
        self.move_type.setCurrentIndex(max(0, self.move_type.findData(r['type'])))
        self.category.setCurrentIndex(r['category'])
        at = self.effect.findData(r['effect'])
        if at < 0:
            self.effect.addItem(f"Effect {r['effect']} (current, expanded-only)", r['effect']); at = self.effect.count() - 1
        self.effect.setCurrentIndex(at)

    def load_item(self):
        if self.item.currentData() is None:
            return
        r = self.project.data_record('item', self.item.currentData())['record']
        self.price.setValue(r['price'])
        for key, w in self.item_rows.items():
            present = key == 'price' or key in r
            self.item_form.setRowVisible(w, present)
            if key in ('hp_restore', 'pp_restore') and present:
                w.setValue(r[key])
            if key in ('ev_gain', 'friendship') and present:
                w.setText('/'.join(map(str, r[key])))

    def load_machine(self):
        state = self.project.composed()
        m = next((x for x in gd.machines(self.project, state) if x['id'] == self.machine.currentData()), None)
        if m:
            self.machine_move.setCurrentIndex(max(0, self.machine_move.findData(m['move'])))

    def load_shop(self):
        name = self.shop.currentData(); self.shop_items.clear()
        row = next((r for r in gd.shop_rows(self.project, self.project.composed()) if r['name'] == name), None)
        self.shop_name.setText(name or ''); self.shop_name.setReadOnly(name is not None)
        for item, label in zip(row['items'], row['item_names']) if row else ():
            self.shop_items.addItem(f'{label} ({item})'); self.shop_items.item(self.shop_items.count() - 1).setData(256, item)
        users = ', '.join(row['users']) if row and row['users'] else 'no clerk yet'
        notes = ' '.join(row['notes']) if row else ''
        self.shop_note.setText(f'Used by {users}. Native mart: funds, bag space and quantity are checked by the game; '
                               f'the price is the item record’s. {notes}'.strip())

    def add_shop_item(self):
        item = self.shop_pick.currentData()
        if item in [self.shop_items.item(i).data(256) for i in range(self.shop_items.count())]:
            raise ValueError('That item is already listed.')
        self.shop_items.addItem(self.shop_pick.currentText()); self.shop_items.item(self.shop_items.count() - 1).setData(256, item)

    def remove_shop_item(self):
        row = self.shop_items.currentRow()
        if row < 0:
            raise ValueError('Select a listed item first.')
        self.shop_items.takeItem(row)

    def stage_shop(self):
        name = self.shop_name.text().strip()
        items = [self.shop_items.item(i).data(256) for i in range(self.shop_items.count())]
        before = self.project.data_record('shop', name)['before'] if name else None
        if items == before:
            raise ValueError('The shop inventory did not change.')
        self.stage({'kind': 'shop', 'name': name, 'before': before, 'items': items})

    def stage_shop_removal(self):
        name = self.shop.currentData()
        if name is None:
            raise ValueError('Choose an existing shop to remove.')
        self.stage({'kind': 'shop', 'name': name, 'before': self.project.data_record('shop', name)['before'], 'items': None})

    def show_catalog(self, *_):
        kind = self.catalog_kind.currentText(); mode = self.catalog_only.currentIndex()
        rows = gd.catalog(self.project, kind, self.catalog_search.text(), limit=600)['entries']
        self.catalog_list.clear()
        for r in rows:
            if mode == 1 and r.get('supported', True) or mode == 2 and not r.get('expanded'):
                continue
            state = 'offered' if r.get('supported', True) else f"refused: {r['reason']}"
            extra = f" · {r['evidence']}" if r.get('evidence') and r.get('supported', True) else ''
            self.catalog_list.addItem(f"{r['id']:>4} {r['name']} · {state}{extra}")

    # ---- staging ------------------------------------------------------------------------------

    def stage(self, operation):
        def key(o): return o['kind'], o.get('id', o.get('index', o.get('name')))
        self.operations = [o for o in self.operations if key(o) != key(operation)] + [operation]
        self.queue.clear()
        self.queue.addItems([f"{o['kind']} {key(o)[1]}" for o in self.operations])
        self.status.setText('Staged · Preview records to validate the batch.')

    def stage_move(self):
        ident = self.move.currentData(); current = self.project.data_record('move', ident)
        want = {'power': self.power.value(), 'accuracy': self.accuracy.value(), 'pp': self.pp.value(),
                'type': self.move_type.currentData(), 'category': self.category.currentData(),
                'priority': self.priority.value(), 'effect': self.effect.currentData(), 'effect_chance': self.chance.value()}
        changes = {k: v for k, v in want.items() if current['record'][k] != v}
        if not changes:
            raise ValueError('No move field changed.')
        self.stage({'kind': 'move', 'id': ident, 'before_sha256': current['before_sha256'], 'changes': changes})

    def stage_item(self):
        ident = self.item.currentData(); current = self.project.data_record('item', ident); r = current['record']
        changes = {}
        if self.price.value() != r['price']:
            changes['price'] = self.price.value()
        for key, w in (('hp_restore', self.hp), ('pp_restore', self.pp_restore)):
            if key in r and w.value() != r[key]:
                changes[key] = w.value()
        for key, w in (('ev_gain', self.evs), ('friendship', self.friendship)):
            if key in r:
                values = [int(v) for v in w.text().split('/')]
                if values != r[key]:
                    changes[key] = values
        if not changes:
            raise ValueError('No item field changed.')
        self.stage({'kind': 'item', 'id': ident, 'before_sha256': current['before_sha256'], 'changes': changes})

    def stage_machine(self):
        state = self.project.composed()
        m = next(x for x in gd.machines(self.project, state) if x['id'] == self.machine.currentData())
        if self.machine_move.currentData() == m['move']:
            raise ValueError('The TM already teaches that move.')
        self.stage({'kind': 'machine', 'index': m['id'], 'before': m['move'], 'move': self.machine_move.currentData()})

    def preview_records(self):
        if not self.operations:
            raise ValueError('Stage a record change first.')
        plan = self.project.plan_data_edit(self.operations, 'Records')
        lines = []
        for p in plan['transactions'][0]['preview']:
            if p['kind'] == 'shop':
                i = p['impact']
                lines.append(f"shop {p['name']}: {p['before']} → {p['after']} · clerks {i['events']} · {i['note']} "
                             + ' '.join(i['notes']))
            elif p['kind'] == 'machine':
                i = p['impact']
                lines.append(f"{p['name']}: {i['before_move']} → {i['after_move']} · {i['compatible_species']} compatible species · {i['field_use']}")
            else:
                changed = {k: (p['before'][k], p['after'][k]) for k in p['after'] if p['before'].get(k) != p['after'][k]}
                i = p['impact']
                users = (f"{i.get('level_up_species', 0)} learnsets, machines {i.get('machines', [])}, "
                         f"trainers {i.get('library_trainers', [])}, tutors {i.get('tutors', [])}" if p['kind'] == 'move'
                         else f"trainers {i.get('library_trainers', [])}, events {i.get('events', [])}")
                lines.append(f"{p['kind']} {p['name']}: {changed} · shared by {users}")
        self.preview.setPlainText('\n'.join(lines)); self.status.setText('Preview validated · Apply records saves one undoable edit.')

    def apply(self):
        if not self.operations:
            raise ValueError('Nothing staged.')
        result = self.project.apply_data_edit(self.revision, self.operations, 'Records')
        self.operations = []; self.queue.clear(); self.reload()
        self.status.setText(f"Applied · revision {result['revision']}.")
        if self.on_applied:
            self.on_applied()

    def discard(self):
        self.operations = []; self.queue.clear(); self.preview.clear(); self.status.setText('Staged records discarded.')
