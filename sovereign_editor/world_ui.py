"""Native world editor: created areas, entrance/return connections, terrain and identity.

Every button builds the same Project operations as `area-edit` (kinds world and
terrain): preview is a dry-run plan, Apply is one atomic, undoable edit.
"""
import copy
import json

from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QPushButton, QTabWidget,
                               QWidget, QComboBox, QSpinBox, QLineEdit, QPlainTextEdit, QListWidget,
                               QListWidgetItem, QCheckBox, QGridLayout, QTableWidget, QTableWidgetItem,
                               QHeaderView, QAbstractItemView)
from PySide6.QtCore import Qt

from . import world_authoring as wa
from .formats import EditorError


def spin(low, high, value):
    widget = QSpinBox()
    widget.setRange(low, high)
    widget.setValue(value)
    return widget


def rects(text):
    """'x,z,w,h; ...' -> the union of those tile rectangles as [{'x','z'}]."""
    tiles = []
    for part in text.replace('\n', ';').split(';'):
        part = part.strip()
        if not part:
            continue
        x, z, w, h = (int(v) for v in part.split(','))
        tiles.extend({'x': xx, 'z': zz} for zz in range(z, z + h) for xx in range(x, x + w))
    unique = {(t['x'], t['z']): t for t in tiles}
    return [unique[k] for k in sorted(unique)]


def pairs(text):
    """'x,z; x,z' -> [{'x':..,'z':..}] for wide entrances."""
    result = []
    for part in text.replace('\n', ';').split(';'):
        part = part.strip()
        if not part:
            continue
        x, _, z = part.partition(',')
        result.append({'x': int(x), 'z': int(z)})
    return result


class WorldEditor(QDialog):
    def __init__(self, inspector):
        super().__init__(inspector)
        self.inspector = inspector
        self.project = inspector.project
        self.context = dict(header=inspector.header, cell=list(inspector.cell))
        self.ctx = self.project.context(**self.context)
        self.operations, self.plan = [], None
        self.setWindowTitle('Sovereign Editor · World')
        self.resize(980, 760)
        layout = QVBoxLayout(self)
        title = QLabel('Areas, connections and terrain')
        title.setStyleSheet('font-size:22px;font-weight:600')
        layout.addWidget(title)
        self.capacity = QLabel('')
        self.capacity.setWordWrap(True)
        layout.addWidget(self.capacity)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.area_panel(), 'Areas')
        self.tabs.addTab(self.connection_panel(), 'Connections')
        self.tabs.addTab(self.terrain_panel(), 'Terrain')
        self.tabs.addTab(self.border_panel(), 'Borders')
        self.tabs.addTab(self.ground_panel(), 'Ground')
        self.tabs.addTab(self.identity_panel(), 'Identity')
        self.tabs.addTab(self.elevation_panel(), 'Elevation')
        self.tabs.addTab(self.travel_panel(), 'Travel')
        self.tabs.addTab(self.effect_panel(), 'Effects')
        self.tabs.addTab(self.group_panel(), 'Groups')
        self.tabs.addTab(self.environment_panel(), 'Environments')
        layout.addWidget(self.tabs, 1)
        layout.addWidget(QLabel('Preview (nothing is saved until Apply)'))
        self.summary = QPlainTextEdit()
        self.summary.setReadOnly(True)
        self.summary.setMaximumHeight(200)
        layout.addWidget(self.summary)
        self.status = QLabel('Preview shows allocated IDs, owned resources and refusals before anything is written.')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        layout.addLayout(buttons)
        buttons.addStretch()
        close = QPushButton('Close')
        close.clicked.connect(self.reject)
        buttons.addWidget(close)
        self.apply_button = QPushButton('Apply')
        self.apply_button.setObjectName('primary')
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.apply)
        buttons.addWidget(self.apply_button)
        self.refresh()

    # ---- panels ----------------------------------------------------------------

    def area_panel(self):
        panel = QWidget()
        layout = QHBoxLayout(panel)
        left = QVBoxLayout()
        left.addWidget(QLabel('Created areas'))
        self.areas = QListWidget()
        left.addWidget(self.areas, 1)
        go = QPushButton('Open selected area')
        go.clicked.connect(self.open_area)
        left.addWidget(go)
        layout.addLayout(left, 1)
        form = QFormLayout()
        self.identity = QLineEdit('')
        self.identity.setPlaceholderText('survey_field')
        form.addRow('Identity', self.identity)
        self.area_name = QLineEdit('')
        form.addRow('Name', self.area_name)
        self.internal_name = QLineEdit('')
        self.internal_name.setPlaceholderText('SURVEY_FIELD (16-byte internal name)')
        form.addRow('Internal name', self.internal_name)
        self.template = spin(0, 65535, self.ctx['header']['id'])
        form.addRow('Template header', self.template)
        # A compact up-to-4x4 layout: each square names its donor as header:x,y
        # (a stock matrix cell). Blank squares are unused; the used squares must
        # fill a rectangle. Rows are Z (north first), columns X.
        self.layout_table = QTableWidget(wa.MAX_SIDE, wa.MAX_SIDE)
        self.layout_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.layout_table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.layout_table.setFixedHeight(150)
        self.layout_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.set_layout_cell(0, 0, f"{self.ctx['header']['id']}:{','.join(map(str, self.context['cell']))}")
        form.addRow('Layout (donor header:x,y)', self.layout_table)
        window = QHBoxLayout()
        self.window_matrix = spin(0, 65535, self.ctx['matrix']['id'])
        self.window_x = spin(0, 255, self.context['cell'][0])
        self.window_y = spin(0, 255, self.context['cell'][1])
        self.window_w = spin(1, wa.MAX_SIDE, 1)
        self.window_h = spin(1, wa.MAX_SIDE, 1)
        for label, widget in (('matrix', self.window_matrix), ('x', self.window_x), ('y', self.window_y),
                              ('w', self.window_w), ('h', self.window_h)):
            window.addWidget(QLabel(label))
            window.addWidget(widget)
        fill = QPushButton('Fill from stock window')
        fill.clicked.connect(self.fill_window)
        window.addWidget(fill)
        form.addRow('Stock window', window)
        self.encounters = QComboBox()
        self.encounters.addItem('Private copy of template encounters', 'template')
        self.encounters.addItem('No encounters (indoor)', 'none')
        form.addRow('Encounters', self.encounters)
        self.worldmap = QLineEdit(f"{self.ctx['cell']['x']},{self.ctx['cell']['y']}")
        form.addRow('World-map position', self.worldmap)
        self.close_edges = QCheckBox('Block the outer edge of the new area')
        self.close_edges.setChecked(True)
        form.addRow('', self.close_edges)
        self.static_cells = QCheckBox('Keep cells whose materials the area animation would move static (like stock forest)')
        self.static_cells.setChecked(True)
        form.addRow('', self.static_cells)
        actions = QHBoxLayout()
        review = QPushButton('Review template')
        review.clicked.connect(self.review_template)
        actions.addWidget(review)
        preview = QPushButton('Preview new area')
        preview.clicked.connect(self.stage_area)
        actions.addWidget(preview)
        form.addRow('', actions)
        note = QLabel('Templates copy visuals only (stock altitudes kept). Events, scripts, map-load scripts and text '
                      'start empty; encounters are a private copy or none. Review lists cross-cell geometry a '
                      'layout loses or gains before anything is written. Donor areas are unchanged.')
        note.setWordWrap(True)
        form.addRow(note)
        holder = QWidget()
        holder.setLayout(form)
        layout.addWidget(holder, 2)
        return panel

    def connection_panel(self):
        panel = QWidget()
        layout = QHBoxLayout(panel)
        left = QVBoxLayout()
        self.here = QLabel('')
        self.here.setWordWrap(True)
        left.addWidget(self.here)
        self.links = QListWidget()
        left.addWidget(self.links, 1)
        follow = QPushButton('Open destination')
        follow.clicked.connect(self.open_destination)
        left.addWidget(follow)
        layout.addLayout(left, 1)
        form = QFormLayout()
        self.source_x = spin(0, 65535, self.ctx['origin'][0])
        self.source_z = spin(0, 65535, self.ctx['origin'][1])
        form.addRow('Entrance x', self.source_x)
        form.addRow('Entrance z', self.source_z)
        self.extra = QLineEdit('')
        self.extra.setPlaceholderText('wide entrance: x,z; x,z')
        form.addRow('Extra entrance tiles', self.extra)
        self.destination = spin(0, 65535, self.ctx['header']['id'])
        self.arrival_extra = QLineEdit('')
        self.arrival_extra.setPlaceholderText('wide arrival: x,z; x,z')
        form.addRow('Destination header', self.destination)
        self.arrival_x = spin(0, 65535, 0)
        self.arrival_z = spin(0, 65535, 0)
        form.addRow('Arrival x', self.arrival_x)
        form.addRow('Arrival z', self.arrival_z)
        form.addRow('Extra arrival tiles', self.arrival_extra)
        preview = QPushButton('Preview connection')
        preview.clicked.connect(self.stage_connection)
        form.addRow('', preview)
        note = QLabel('Both ends need a stock door (0x69, blocked), exit mat (0x65), gate opening (0x6E) or cave '
                      'exit (0x6F) with a walkable step. The return warp is created with the entrance; both ends may '
                      'be in one area (cave holes).')
        note.setWordWrap(True)
        form.addRow(note)
        # Move an existing connection's ends in place (warp IDs kept, so both ends stay paired).
        self.move_connection = QComboBox()
        form.addRow('Move connection', self.move_connection)
        self.move_source = QLineEdit('')
        self.move_source.setPlaceholderText('new entrance tiles: x,z; x,z   (blank = keep)')
        self.move_target = QLineEdit('')
        self.move_target.setPlaceholderText('new arrival tiles: x,z; x,z   (blank = keep)')
        form.addRow('New entrance tiles', self.move_source)
        form.addRow('New arrival tiles', self.move_target)
        move = QPushButton('Preview entrance move')
        move.clicked.connect(self.stage_move)
        form.addRow('', move)
        holder = QWidget()
        holder.setLayout(form)
        layout.addWidget(holder, 1)
        return panel

    def border_panel(self):
        panel = QWidget()
        form = QFormLayout(panel)
        self.border_family = QComboBox()
        self.border_family.addItem('Path with stock rims (road01 / road01_r)', 'path')
        self.border_family.addItem('Tall grass with stock borders (egrass family)', 'tall_grass')
        form.addRow('Border', self.border_family)
        self.border_tiles = QLineEdit('')
        self.border_tiles.setPlaceholderText('x,z,w,h; x,z,w,h   (global tiles; rectangles are joined)')
        form.addRow('Tiles', self.border_tiles)
        self.border_window = QLineEdit('')
        self.border_window.setPlaceholderText('optional x,z,w,h: reroute stock road inside it (paths only)')
        form.addRow('Window', self.border_window)
        # Custom ground materials (Ground tab): paving over the union with existing paving,
        # grass/dirt rims around it, and erase back to lawn.
        self.border_material = QComboBox()
        self.border_material.addItem('Stock dirt path (road01)', None)
        for mid, m in self.ground_materials().items():
            self.border_material.addItem(f"{m['display']} ({mid})", mid)
        form.addRow('Path material', self.border_material)
        self.border_erase = QCheckBox('Erase these tiles from the material (rims follow)')
        form.addRow('', self.border_erase)
        self.border_decorative = QCheckBox('Decorative tall grass: stock look on ordinary walkable ground (no encounters, no rustle)')
        form.addRow('', self.border_decorative)
        preview = QPushButton('Preview border')
        preview.clicked.connect(self.stage_border)
        form.addRow('', preview)
        note = QLabel('Edges, outer and inner corners follow the stock pieces and continue across cell seams. '
                      'Paths walk 0x00; tall grass gets encounter behavior 0x02 (the area needs an encounter table), or '
                      'ordinary walkable ground when decorative. '
                      'A window clears stock road and rims to grass, lays the new path and caps the cut ends. '
                      'Shapes the stock pieces cannot draw, tiles under raised scenery and borders leaving the area '
                      'are refused before anything is written.')
        note.setWordWrap(True)
        form.addRow(note)
        return panel

    def travel_panel(self):
        """Respawn and Fly points (TRAVEL-01, FIELD-05): the same area-edit operation as the CLI."""
        panel = QWidget()
        form = QFormLayout(panel)
        self.travel_list = QPlainTextEdit()
        self.travel_list.setReadOnly(True)
        self.travel_list.setMaximumHeight(120)
        form.addRow('Points', self.travel_list)
        self.travel_action = QComboBox()
        self.travel_action.addItems(['define', 'revise', 'retire'])
        self.travel_key = QLineEdit('')
        self.travel_key.setPlaceholderText('ranger_station')
        self.travel_name = QLineEdit('')
        for label, widget in (('Action', self.travel_action), ('Key', self.travel_key), ('Name', self.travel_name)):
            form.addRow(label, widget)
        self.travel_arrival = [spin(0, 65535, self.ctx['header']['id']), spin(0, 65535, self.ctx['origin'][0] + 16),
                               spin(0, 65535, self.ctx['origin'][1] + 16)]
        row = QHBoxLayout()
        for label, widget in zip(('header', 'x', 'z'), self.travel_arrival):
            row.addWidget(QLabel(label)); row.addWidget(widget)
        form.addRow('Arrival (Fly / Teleport)', row)
        self.travel_respawn_same = QCheckBox('Respawn on the arrival tile')
        self.travel_respawn = [spin(0, 65535, self.ctx['header']['id']), spin(0, 255, 8), spin(0, 255, 8)]
        row = QHBoxLayout()
        row.addWidget(self.travel_respawn_same)
        for label, widget in zip(('header', 'x', 'z'), self.travel_respawn):
            row.addWidget(QLabel(label)); row.addWidget(widget)
        form.addRow('Respawn (after a loss)', row)
        self.travel_blackout = QCheckBox('Respawn point (entering its respawn map makes it the blackout point)')
        self.travel_fly = QCheckBox('Fly destination (discovered on the first visit; needs the Pokégear position)')
        form.addRow('', self.travel_blackout)
        form.addRow('', self.travel_fly)
        self.travel_message = QPlainTextEdit()
        self.travel_message.setPlaceholderText('Arrival pages after a loss; separate pages with a line ---')
        self.travel_message.setMaximumHeight(80)
        form.addRow('Arrival message', self.travel_message)
        button = QPushButton('Preview travel point')
        button.clicked.connect(self.stage_travel)
        form.addRow('', button)
        self.field_action = QComboBox()
        self.field_action.addItems(['place', 'remove'])
        self.field_key = QLineEdit('')
        self.field_key.setPlaceholderText('bay_whirlpool')
        self.field_x = spin(0, 65535, self.ctx['origin'][0] + 16)
        self.field_z = spin(0, 65535, self.ctx['origin'][1] + 16)
        row = QHBoxLayout()
        for label, widget in (('action', self.field_action), ('key', self.field_key), ('x', self.field_x),
                              ('z', self.field_z)):
            row.addWidget(QLabel(label)); row.addWidget(widget)
        form.addRow('Whirlpool (3x3 on Surf water; x, z = NW tile)', row)
        button = QPushButton('Preview whirlpool')
        button.clicked.connect(self.stage_field)
        form.addRow('', button)
        note = QLabel('Spawn IDs and Fly flags are permanent (saves may hold them): points retire instead of being '
                      'deleted. A respawn map needs its own script and text banks for the arrival script; Fly '
                      'destinations use five qualified flypoint flags and the Storm Badge like stock towns.')
        note.setWordWrap(True)
        form.addRow(note)
        self.refresh_travel()
        return panel

    def refresh_travel(self):
        try:
            view = self.project.travel_view()
        except EditorError as exc:
            self.travel_list.setPlainText(f'Travel points unavailable: {exc}')
            return
        lines = [f"{p['key']} · spawn {p['spawn']} · {p['name']} · arrives {p['arrival_name']} "
                 f"{p['arrival']['x']},{p['arrival']['z']}"
                 + (' · respawn' if p['blackout'] else '') + (f" · Fly flag {p['flag']:#x}" if p['fly'] else '')
                 + (' · retired' if p['retired'] else '') for p in view['points']]
        cap = view['capacity']
        lines.append(f"capacity: points {cap['points'][0]}/{cap['points'][1]} · Fly {cap['fly'][0]}/{cap['fly'][1]}")
        self.travel_list.setPlainText('\n'.join(lines))

    def stage_travel(self):
        action = self.travel_action.currentText()
        request = {'action': action, 'key': self.travel_key.text().strip()}
        if action != 'retire':
            pages = [p.strip('\n') for p in self.travel_message.toPlainText().split('\n---\n') if p.strip()]
            request.update(name=self.travel_name.text().strip(),
                           arrival=dict(zip(('header', 'x', 'z'), (w.value() for w in self.travel_arrival))),
                           respawn=None if self.travel_respawn_same.isChecked() else
                           dict(zip(('header', 'x', 'z'), (w.value() for w in self.travel_respawn))),
                           blackout=self.travel_blackout.isChecked(), fly=self.travel_fly.isChecked(), message=pages)
        self.stage([{'kind': 'travel', 'context': self.context, 'request': request}], f'Travel point {action}')

    def stage_field(self):
        action = self.field_action.currentText()
        request = {'action': action, 'key': self.field_key.text().strip()}
        if action == 'place':
            request.update(family='whirlpool', x=self.field_x.value(), z=self.field_z.value())
        self.stage([{'kind': 'field', 'context': self.context, 'request': request}], f'Whirlpool {action}')

    def effect_panel(self):
        """Airborne petals (EFFECT-01): the same area-edit operation as the CLI (kind petals)."""
        panel = QWidget()
        outer = QVBoxLayout(panel)
        form = QFormLayout()
        outer.addLayout(form)
        outer.addStretch(1)
        self.effect_list = QPlainTextEdit()
        self.effect_list.setReadOnly(True)
        self.effect_list.setMaximumHeight(140)
        form.addRow('Petal areas', self.effect_list)
        self.effect_action = QComboBox()
        self.effect_action.addItems(['set', 'remove'])
        form.addRow('Action', self.effect_action)
        self.effect_header = spin(0, 65535, self.ctx['header']['id'])
        form.addRow('Map header', self.effect_header)
        from . import petal_effect
        self.effect_fields = {}
        for name, hint in (('density', 'petals on screen = 4 x density'),
                           ('speed', 'drift speed; faster petals also spin faster'),
                           ('direction', 'degrees from straight down; negative drifts left')):
            lo, hi = petal_effect.LIMITS[name]
            widget = spin(lo, hi, petal_effect.DEFAULT[name])
            widget.setMaximumWidth(90)
            row = QHBoxLayout()
            row.addWidget(widget)
            row.addWidget(QLabel(hint))
            row.addStretch(1)
            form.addRow(name.capitalize(), row)
            self.effect_fields[name] = widget
        button = QPushButton('Preview petals')
        button.clicked.connect(self.stage_effect)
        form.addRow('', button)
        note = QLabel('Petals are field weather 14 on the chosen header: they replace its weather (rain, snow) while '
                      'set and restore it when removed. Headers that use weather for cave lighting (Flash) are '
                      'refused. The effect draws on the weather sprite layer over the map, player and follower; it '
                      'never changes map models, so canopies, ground petals and water keep their own look.')
        note.setWordWrap(True)
        form.addRow(note)
        # Qualified base-ROM repairs (R101-SHOP): the same area-edit kind `repair` as the CLI.
        self.repair_list = QPlainTextEdit()
        self.repair_list.setReadOnly(True)
        self.repair_list.setMaximumHeight(80)
        form.addRow('Base-ROM repairs', self.repair_list)
        self.repair_key = QComboBox()
        from . import runtime_repairs
        self.repair_key.addItems(list(runtime_repairs.REPAIRS))
        repair_row = QHBoxLayout()
        repair_row.addWidget(self.repair_key)
        for action in runtime_repairs.ACTIONS:
            b = QPushButton(f'Preview {action}')
            b.clicked.connect(lambda _=False, a=action: self.stage_repair(a))
            repair_row.addWidget(b)
        repair_row.addStretch(1)
        form.addRow('Repair', repair_row)
        self.refresh_effects()
        return panel

    def stage_repair(self, action):
        request = {'action': action, 'repair': self.repair_key.currentText()}
        self.stage([{'kind': 'repair', 'context': self.context, 'request': request}], f'Repair {action}')

    def refresh_effects(self):
        try:
            view = self.project.petal_view()
        except EditorError as exc:
            self.effect_list.setPlainText(f'Petal effect unavailable: {exc}')
            return
        lines = [f"{a['name']} (header {a['header']}) · density {a['density']} speed {a['speed']} direction "
                 f"{a['direction']}° · ~{a['runtime']['on_screen']} petals, {a['runtime']['seconds_to_cross']} s to "
                 f"cross · replaces weather {a['weather_replaced']}" for a in view['areas']]
        lines.append(f"capacity: areas {view['capacity']['areas'][0]}/{view['capacity']['areas'][1]} · "
                     f"{view['capacity']['sprites']} weather sprites")
        self.effect_list.setPlainText('\n'.join(lines))
        if hasattr(self, 'repair_list'):
            try:
                repairs = self.project.repair_view()['repairs']
                self.repair_list.setPlainText('\n'.join(
                    f"{r['repair']}: {r['name']} ({r['issue']}) · {'applied' if r['applied'] else 'not applied'}"
                    f"{'' if r['available'] else ' · unavailable: ' + r['report']['message']}" for r in repairs))
            except (EditorError, AttributeError) as exc:
                self.repair_list.setPlainText(f'Repairs unavailable: {exc}')

    def stage_effect(self):
        action = self.effect_action.currentText()
        request = {'action': action, 'header': self.effect_header.value()}
        if action == 'set':
            request.update({k: w.value() for k, w in self.effect_fields.items()})
        self.stage([{'kind': 'petals', 'context': self.context, 'request': request}], f'Petals {action}')

    def group_panel(self):
        """Reusable map groups (GROUP-01/02): capture from this cell, copy, move and remove as one edit."""
        from . import map_groups, props, scenery, simple_interactions as si
        panel = QWidget()
        outer = QVBoxLayout(panel)
        self.group_list = QPlainTextEdit(); self.group_list.setReadOnly(True); self.group_list.setMaximumHeight(110)
        outer.addWidget(self.group_list)
        row = QHBoxLayout(); outer.addLayout(row)
        capture = QFormLayout(); row.addLayout(capture, 3)
        self.group_name = QLineEdit(''); self.group_name.setPlaceholderText('blossom_grove')
        capture.addRow('Template name', self.group_name)
        self.group_anchor = [spin(0, 65535, self.ctx['origin'][0] + 16), spin(0, 65535, self.ctx['origin'][1] + 16)]
        anchor = QHBoxLayout()
        for label, w in zip(('x', 'z'), self.group_anchor):
            anchor.addWidget(QLabel(label)); anchor.addWidget(w)
        capture.addRow('Anchor tile', anchor)
        state = self.project.composed()
        ref = self.ctx
        self.group_objects, self.group_props, self.group_signs = QListWidget(), QListWidget(), QListWidget()
        for widget in (self.group_objects, self.group_props, self.group_signs):
            widget.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
            widget.setMaximumHeight(90)
        from . import authoring
        for slot, obj in sorted(scenery.table_for(self.project, ref, state).items()):
            if obj['id'].startswith('prop:'):
                continue
            pos = authoring.global_from_record(ref, scenery.words(obj['raw']))
            item = QListWidgetItem(f"object {slot} · model {obj['raw'][0]} · {pos['x']:.1f},{pos['z']:.1f}")
            item.setData(Qt.ItemDataRole.UserRole, slot); self.group_objects.addItem(item)
        cref = authoring.context_ref(ref)
        for key, inst in sorted(props.instances(state).items()):
            if inst['context'] == cref:
                item = QListWidgetItem(f"{key} · {inst['asset']} · {inst['x']},{inst['z']}")
                item.setData(Qt.ItemDataRole.UserRole, key); self.group_props.addItem(item)
        for key, spec in sorted(si.specs(state).items()):
            if spec['context'] == cref:
                item = QListWidgetItem(f"{key} · {spec['kind']} · {spec['x']},{spec['z']}")
                item.setData(Qt.ItemDataRole.UserRole, key); self.group_signs.addItem(item)
        for label, widget in (('Stock objects', self.group_objects), ('Prop instances', self.group_props),
                              ('Signs / NPCs', self.group_signs)):
            capture.addRow(label, widget)
        self.group_rect = QLineEdit(''); self.group_rect.setPlaceholderText('tiles: x0,z0,x1,z1 (optional)')
        self.group_entrance = QLineEdit(''); self.group_entrance.setPlaceholderText('entrance tile x,z (optional)')
        capture.addRow('Tiles', self.group_rect); capture.addRow('Entrance', self.group_entrance)
        button = QPushButton('Preview capture'); button.clicked.connect(self.stage_group_capture)
        capture.addRow('', button)
        use = QFormLayout(); row.addLayout(use, 2)
        self.group_template = QComboBox(); use.addRow('Template', self.group_template)
        self.group_instance = QLineEdit(''); self.group_instance.setPlaceholderText('grove_east')
        use.addRow('Copy / instance', self.group_instance)
        self.group_target = [spin(0, 65535, self.ctx['origin'][0] + 16), spin(0, 65535, self.ctx['origin'][1] + 16)]
        target = QHBoxLayout()
        for label, w in zip(('x', 'z'), self.group_target):
            target.addWidget(QLabel(label)); target.addWidget(w)
        use.addRow('Anchor in this cell', target)
        self.group_destination = QLineEdit(''); self.group_destination.setPlaceholderText('header,x,z of an unused exit mat')
        use.addRow('Entrance leads to', self.group_destination)
        for label, handler in (('Preview copy here', self.stage_group_place), ('Preview move', self.stage_group_move),
                               ('Preview remove', self.stage_group_remove)):
            b = QPushButton(label); b.clicked.connect(handler); use.addRow('', b)
        note = QLabel('Shared: stock models/textures and prop assets. Copied: placements, tiles, sign/NPC text. '
                      'A copied building never shares an interior: name an unused exit mat for its own two-way '
                      'connection, or leave its door unconnected. Moves stay in one cell; one Apply is one undo.')
        note.setWordWrap(True); outer.addWidget(note)
        outer.addStretch(1)
        self.refresh_groups()
        return panel

    def environment_panel(self):
        """Reusable named environments (KIT-02): define/revise/retire as JSON, place presets here."""
        panel = QWidget()
        outer = QVBoxLayout(panel)
        self.env_list = QPlainTextEdit(); self.env_list.setReadOnly(True); self.env_list.setMaximumHeight(120)
        outer.addWidget(self.env_list)
        row = QHBoxLayout(); outer.addLayout(row)
        edit = QFormLayout(); row.addLayout(edit, 3)
        self.env_action = QComboBox(); self.env_action.addItems(['define', 'revise', 'retire'])
        self.env_key = QLineEdit(''); self.env_key.setPlaceholderText('cg_town')
        edit.addRow('Action', self.env_action); edit.addRow('Environment key', self.env_key)
        self.env_spec = QPlainTextEdit()
        self.env_spec.setPlaceholderText('{"display": ..., "kind": "outdoor", "ground": [...], "props": [...], '
                                         '"presets": {"raised_street": {"type": "terrace", ...}}, "donors": {}}')
        edit.addRow('Fields (JSON)', self.env_spec)
        load = QPushButton('Load the selected environment'); load.clicked.connect(self.load_environment)
        preview = QPushButton('Preview environment edit'); preview.clicked.connect(self.stage_environment)
        edit.addRow('', load); edit.addRow('', preview)
        place = QFormLayout(); row.addLayout(place, 2)
        self.env_pick = QComboBox(); self.env_pick.currentIndexChanged.connect(self.fill_env_presets)
        self.env_preset = QComboBox()
        place.addRow('Environment', self.env_pick); place.addRow('Preset', self.env_preset)
        self.env_at = [spin(0, 65535, self.ctx['origin'][0] + 16), spin(0, 65535, self.ctx['origin'][1] + 16)]
        at = QHBoxLayout()
        for label, w in zip(('x', 'z'), self.env_at):
            at.addWidget(QLabel(label)); at.addWidget(w)
        place.addRow('Anchor in this area', at)
        self.env_place_key = QLineEdit(''); self.env_place_key.setPlaceholderText('grove2 (props/decals)')
        place.addRow('Copy key', self.env_place_key)
        self.env_area = [QLineEdit(''), QLineEdit(''), QLineEdit('')]
        for w, hint in zip(self.env_area, ('identity', 'Name', 'INTERNAL_NAME')):
            w.setPlaceholderText(hint)
        area = QHBoxLayout()
        for w in self.env_area:
            area.addWidget(w)
        place.addRow('New area (area presets)', area)
        button = QPushButton('Preview placement'); button.clicked.connect(self.stage_environment_place)
        place.addRow('', button)
        note = QLabel('A placement becomes the ordinary terrain, paving, prop, decal or area operations, each '
                      'checked by its own rules; one Apply is one undo. Revising an environment changes what later '
                      'placements use, not earlier ones.')
        note.setWordWrap(True); outer.addWidget(note)
        outer.addStretch(1)
        self.refresh_environments()
        return panel

    def refresh_environments(self):
        view = self.project.environment_view()['environments']
        self.env_list.setPlainText('\n'.join(
            f"{k} · r{v['revision']}{' · retired' if v['retired'] else ''} · {v['display']} ({v['kind']}) · ground "
            f"{v['ground']} · props {v['props']} · presets " + ', '.join(n + ':' + p['type'] for n, p in v['presets'].items())
            for k, v in view.items()) or 'No environments yet.')
        current = self.env_pick.currentData()
        self.env_pick.blockSignals(True); self.env_pick.clear()
        for k, v in view.items():
            if not v['retired']:
                self.env_pick.addItem(f"{v['display']} ({k})", k)
        self.env_pick.blockSignals(False)
        if current is not None and self.env_pick.findData(current) >= 0:
            self.env_pick.setCurrentIndex(self.env_pick.findData(current))
        self.fill_env_presets()

    def fill_env_presets(self, *_):
        self.env_preset.clear()
        env = self.project.environment_view()['environments'].get(self.env_pick.currentData())
        for name, p in (env or {}).get('presets', {}).items():
            self.env_preset.addItem(f"{name} ({p['type']})", name)

    def load_environment(self):
        from . import environments
        env = environments.catalog(self.project.composed()).get(self.env_pick.currentData())
        if env is None:
            return self.fail('Choose an environment')
        self.env_key.setText(self.env_pick.currentData())
        self.env_spec.setPlainText(json.dumps({k: env[k] for k in environments.FIELDS}, indent=1))
        self.env_action.setCurrentText('revise')

    def stage_environment(self):
        from . import environments
        action, key = self.env_action.currentText(), self.env_key.text().strip()
        try:
            fields = json.loads(self.env_spec.toPlainText() or '{}') if action != 'retire' else {}
        except ValueError as exc:
            return self.fail(f'Fields are not valid JSON: {exc}')
        request = {'action': action, 'key': key, **fields}
        if action != 'define':
            env = environments.catalog(self.project.composed()).get(key)
            request['expected_revision'] = env['revision'] if env else None
        self.stage([{'kind': 'environment', 'context': self.context, 'request': request}], f'Environment {action}')

    def stage_environment_place(self):
        request = {'action': 'place', 'environment': self.env_pick.currentData(), 'preset': self.env_preset.currentData(),
                   'x': self.env_at[0].value(), 'z': self.env_at[1].value()}
        if self.env_place_key.text().strip():
            request['key'] = self.env_place_key.text().strip()
        identity, name, internal = (w.text().strip() for w in self.env_area)
        if identity:
            request.update(identity=identity, name=name, internal_name=internal)
            request.pop('x'); request.pop('z')
        self.stage([{'kind': 'environment', 'context': self.context, 'request': request}], 'Place environment preset')

    def refresh_groups(self):
        view = self.project.map_group_view()
        lines = [f"template {k} · {v['source']['id']} · {v['report']['copied']} · shared "
                 f"{v['report']['shared'] + v['report']['shared_assets']}" for k, v in view['templates'].items()]
        lines += [f"copy {k} of {v['template']} · {v['context']['id']} anchor {v['anchor']['x']},{v['anchor']['z']}"
                  + (f" · connection {v['connection']}" if v['connection'] is not None else '')
                  for k, v in view['instances'].items()]
        self.group_list.setPlainText('\n'.join(lines) or 'No groups yet.')
        self.group_template.clear()
        for k in view['templates']:
            self.group_template.addItem(k, k)

    def _ints(self, widget, count):
        text = widget.text().strip()
        if not text:
            return None
        values = [int(v) for v in text.split(',')]
        if len(values) != count:
            raise ValueError(f'Expected {count} comma-separated numbers')
        return values

    def stage_group_capture(self):
        try:
            rect = self._ints(self.group_rect, 4)
            door = self._ints(self.group_entrance, 2)
        except ValueError as exc:
            self.status.setText(str(exc)); return
        pick = lambda w: [i.data(Qt.ItemDataRole.UserRole) for i in w.selectedItems()]
        request = {'action': 'capture', 'name': self.group_name.text().strip(),
                   'anchor': {'x': self.group_anchor[0].value(), 'z': self.group_anchor[1].value()},
                   'objects': pick(self.group_objects), 'props': pick(self.group_props),
                   'interactions': pick(self.group_signs)}
        if rect:
            x0, z0, x1, z1 = rect
            request['cells'] = [{'x': x, 'z': z} for x in range(min(x0, x1), max(x0, x1) + 1)
                                for z in range(min(z0, z1), max(z0, z1) + 1)]
        if door:
            request['entrances'] = [{'x': door[0], 'z': door[1]}]
        self.stage([{'kind': 'map_group', 'context': self.context, 'request': request}], 'Group capture')

    def stage_group_place(self):
        try:
            dest = self._ints(self.group_destination, 3)
        except ValueError as exc:
            self.status.setText(str(exc)); return
        request = {'action': 'place', 'name': self.group_template.currentData(),
                   'instance': self.group_instance.text().strip(),
                   'x': self.group_target[0].value(), 'z': self.group_target[1].value()}
        if dest:
            request['entrance'] = {'destination': {'header': dest[0]}, 'arrival': {'x': dest[1], 'z': dest[2]}}
        self.stage([{'kind': 'map_group', 'context': self.context, 'request': request}], 'Group copy')

    def stage_group_move(self):
        request = {'action': 'move', 'instance': self.group_instance.text().strip(),
                   'x': self.group_target[0].value(), 'z': self.group_target[1].value()}
        self.stage([{'kind': 'map_group', 'context': self.context, 'request': request}], 'Group move')

    def stage_group_remove(self):
        request = {'action': 'remove', 'instance': self.group_instance.text().strip()}
        self.stage([{'kind': 'map_group', 'context': self.context, 'request': request}], 'Group remove')

    def ground_materials(self):
        try:
            return self.project.ground_view()['materials']
        except EditorError:
            return {}

    def ground_panel(self):
        from PySide6.QtWidgets import QFileDialog, QDoubleSpinBox
        panel = QWidget()
        form = QFormLayout(panel)
        self.ground_list = QPlainTextEdit()
        self.ground_list.setReadOnly(True)
        self.ground_list.setMaximumHeight(110)
        form.addRow('Materials', self.ground_list)
        self.refresh_ground()
        row = QHBoxLayout()
        self.ground_source = QLineEdit('')
        self.ground_source.setPlaceholderText('folder with material.json + indexed PNGs (sovereign-ground-material-v1)')
        browse = QPushButton('Choose…')
        browse.clicked.connect(lambda: self.ground_source.setText(
            QFileDialog.getExistingDirectory(self, 'Ground material folder') or self.ground_source.text()))
        row.addWidget(self.ground_source)
        row.addWidget(browse)
        form.addRow('Import / reimport', row)
        button = QPushButton('Preview import')
        button.clicked.connect(self.stage_ground_import)
        form.addRow('', button)
        self.variant_material = QComboBox()
        self.variant_area = spin(0, 65535, self.ctx['area_data']['id'])
        self.variant_name = QLineEdit('base')
        for label, widget in (('Variant material', self.variant_material), ('Area data', self.variant_area),
                              ('Variant', self.variant_name)):
            form.addRow(label, widget)
        button = QPushButton('Preview variant')
        button.clicked.connect(self.stage_ground_variant)
        form.addRow('', button)
        self.decal_action = QComboBox()
        self.decal_action.addItems(['place', 'move', 'duplicate', 'erase', 'revise'])
        self.decal_key = QLineEdit('')
        self.decal_source = QLineEdit('')
        self.decal_material = QComboBox()
        self.decal_name = QLineEdit('')
        self.decal_name.setPlaceholderText('decal key in the material, e.g. pile')
        self.decal_x = QDoubleSpinBox(); self.decal_z = QDoubleSpinBox()
        for w, v in ((self.decal_x, self.ctx['origin'][0] + 16), (self.decal_z, self.ctx['origin'][1] + 16)):
            w.setRange(0, 65535); w.setDecimals(4); w.setSingleStep(0.0625); w.setValue(v)
        self.decal_rotation = QComboBox(); self.decal_rotation.addItems(['0', '90', '180', '270'])
        self.decal_flip = QCheckBox('Mirror')
        self.decal_layer = spin(0, 3, 0)
        for label, widget in (('Decal action', self.decal_action), ('Decal key', self.decal_key),
                              ('Duplicate from', self.decal_source), ('Decal material', self.decal_material),
                              ('Decal', self.decal_name), ('Centre x (tiles)', self.decal_x),
                              ('Centre z (tiles)', self.decal_z), ('Rotation', self.decal_rotation),
                              ('', self.decal_flip), ('Layer', self.decal_layer)):
            form.addRow(label, widget)
        button = QPushButton('Preview decal')
        button.clicked.connect(self.stage_decal)
        form.addRow('', button)
        note = QLabel('Ground materials add Project-owned textures to the map tileset of the areas that show them '
                      '(stock textures stay byte-identical; other areas are unaffected) and a per-area variant. '
                      'Decals lie one model unit over the ground, never block, and drape over grass, paving and slopes.')
        note.setWordWrap(True)
        form.addRow(note)
        return panel

    def refresh_ground(self):
        materials = self.ground_materials()
        lines = []
        for mid, m in materials.items():
            lines.append(f"{mid} r{m['revision']} · {m['display']} · roles {', '.join(m['roles'])} · variants "
                         f"{', '.join(m['variants'])} · area variants {m['area_variants'] or 'none'}")
            for u in m['users']:
                lines.append(f"    map {u['map_member']}: " + ', '.join(f'{k} {v}' for k, v in u.items() if k != 'map_member'))
        self.ground_list.setPlainText('\n'.join(lines) or 'No ground materials yet.')
        for combo in (getattr(self, 'variant_material', None), getattr(self, 'decal_material', None)):
            if combo is not None:
                combo.clear()
                for mid in materials:
                    combo.addItem(mid, mid)

    def stage_ground_import(self):
        folder = self.ground_source.text().strip()
        if not folder:
            return self.fail('Choose a ground-material folder')
        self.stage([{'kind': 'ground', 'context': self.context, 'request': {'source': folder}}], 'Ground material import')

    def stage_ground_variant(self):
        self.stage([{'kind': 'ground', 'context': self.context, 'request': {
            'action': 'variant', 'material': self.variant_material.currentData(), 'area_data': self.variant_area.value(),
            'variant': self.variant_name.text().strip()}}], 'Ground material variant')

    def stage_decal(self):
        action = self.decal_action.currentText()
        request = {'action': action, 'key': self.decal_key.text().strip()}
        if action == 'place':
            request.update(material=self.decal_material.currentData(), decal=self.decal_name.text().strip(),
                           x=self.decal_x.value(), z=self.decal_z.value(), rotation=int(self.decal_rotation.currentText()),
                           flip=self.decal_flip.isChecked(), layer=self.decal_layer.value())
        elif action in ('move', 'duplicate'):
            request.update(x=self.decal_x.value(), z=self.decal_z.value())
            if action == 'duplicate':
                request['source'] = self.decal_source.text().strip()
        elif action == 'revise':
            request.update(rotation=int(self.decal_rotation.currentText()), flip=self.decal_flip.isChecked(),
                           layer=self.decal_layer.value())
            if self.decal_name.text().strip():
                request['decal'] = self.decal_name.text().strip()
        self.stage([{'kind': 'decal', 'context': self.context, 'request': request}], f'Ground decal {action}')

    def terrain_panel(self):
        panel = QWidget()
        form = QFormLayout(panel)
        self.use_selection = QCheckBox('Use tiles selected in the map (collision selection)')
        form.addRow('', self.use_selection)
        self.t_x = spin(0, 65535, self.ctx['origin'][0])
        self.t_z = spin(0, 65535, self.ctx['origin'][1])
        self.t_w = spin(1, 16, 1)
        self.t_h = spin(1, 16, 1)
        for label, widget in (('x', self.t_x), ('z', self.t_z), ('Width', self.t_w), ('Height', self.t_h)):
            form.addRow(label, widget)
        self.material = QComboBox()
        form.addRow('Visible ground', self.material)
        self.ground = QComboBox()
        for text, value in (('Unchanged', None), ('Path / ordinary ground (0x00)', 'path'),
                            ('Tall grass: wild encounters (0x02)', 'grass')):
            self.ground.addItem(text, value)
        form.addRow('Encounter behavior', self.ground)
        self.stamp = QCheckBox('Copy collision and behavior from a template rectangle')
        form.addRow('', self.stamp)
        self.stamp_header = spin(0, 65535, self.ctx['header']['id'])
        self.stamp_cell = QLineEdit(','.join(map(str, self.context['cell'])))
        self.stamp_x = spin(0, 65535, self.ctx['origin'][0])
        self.stamp_z = spin(0, 65535, self.ctx['origin'][1])
        for label, widget in (('Template header', self.stamp_header), ('Template cell', self.stamp_cell),
                              ('Template x', self.stamp_x), ('Template z', self.stamp_z)):
            form.addRow(label, widget)
        self.blocked = QComboBox()
        for text, value in (('Unchanged', None), ('Walkable', False), ('Blocked', True)):
            self.blocked.addItem(text, value)
        form.addRow('Collision', self.blocked)
        preview = QPushButton('Preview terrain')
        preview.clicked.connect(self.stage_terrain)
        form.addRow('', preview)
        note = QLabel('Up to 256 tiles per edit. A stamp copies a stock footprint (e.g. a gatehouse) exactly. '
                      'Tall-grass behavior must match visible tall grass and needs an '
                      'encounter table. Entrances, water, ledges and baked scenery are refused. The edited model '
                      'must stay within the measured stock geometry budget.')
        note.setWordWrap(True)
        form.addRow(note)
        return panel

    def elevation_panel(self):
        """Terrain authoring v1: terraces with cliff rims and stairs, ponds, and sound plates."""
        panel = QWidget()
        form = QFormLayout(panel)
        self.el_action = QComboBox()
        for text, value in (('Terrace: raise ground one stock step with cliff rim', 'terrace'),
                            ('Pond: stock water surface and shore', 'pond'),
                            ('Ambient: remove sound plates', 'ambient'),
                            ('Shaped terrace: rectangles, inner corners, level 2, ledges', 'terrace_shape'),
                            ('Shaped water: rectangles with concave shores (river bends)', 'water_shape'),
                            ('Cave rooms: carve floor, walls and exits in a cave area', 'cave_room'),
                            ('Waterfall: raised pool, 0x13 fall row and lower Surf pool', 'waterfall')):
            self.el_action.addItem(text, value)
        form.addRow('Feature', self.el_action)
        self.el_form = form
        self.el_x = spin(0, 65535, self.ctx['origin'][0])
        self.el_z = spin(0, 65535, self.ctx['origin'][1])
        self.el_w = spin(1, 12, 4)
        self.el_h = spin(1, 12, 4)
        for label, widget in (('x (terrace top / pond)', self.el_x), ('z', self.el_z), ('Width', self.el_w),
                              ('Height', self.el_h)):
            form.addRow(label, widget)
        self.el_upper = spin(2, 6, 2)
        self.el_lower = spin(2, 6, 2)
        form.addRow('Upper pool depth (waterfall)', self.el_upper)
        form.addRow('Lower pool depth (waterfall)', self.el_lower)
        self.el_access = QLineEdit('south:0')
        self.el_access.setPlaceholderText('stairs: side:offset; side:offset   (north/south/east/west, 3 wide)')
        form.addRow('Stairs', self.el_access)
        # Shapes (terrain v2): union of rectangles; stairs by their first tile; one-way ledges.
        self.el_rects = QLineEdit('')
        self.el_rects.setPlaceholderText('x,z,width,height; x,z,width,height   (global tiles, one connected shape)')
        form.addRow('Rectangles', self.el_rects)
        self.el_level = spin(1, 2, 1)
        form.addRow('Level (2 = on a level-1 top)', self.el_level)
        self.el_shape_access = QLineEdit('')
        self.el_shape_access.setPlaceholderText('side:x,z; ...   (3-wide stair starting at its smallest tile)')
        form.addRow('Stairs (by tile)', self.el_shape_access)
        self.el_ledges = QLineEdit('')
        self.el_ledges.setPlaceholderText('side:x,z,length; ...   (one-way jump off a straight rim run)')
        form.addRow('Ledges', self.el_ledges)
        self.el_climbs = QLineEdit('')
        self.el_climbs.setPlaceholderText('side:x,z; ...   (Rock Climb face on one straight rim tile)')
        form.addRow('Rock Climb', self.el_climbs)
        self.el_exits = QLineEdit('')
        self.el_exits.setPlaceholderText('x,z; ...   (floor tile on a straight south wall of five tiles)')
        form.addRow('Cave exits', self.el_exits)
        # Revise a carved cave (same rooms, new exits/encounters) or a waterfall (same pools, current
        # geometry version) in place; the recorded feature's history is kept.
        self.el_replaces = QComboBox()
        self.el_replaces.addItem('New feature', None)
        form.addRow('Revise', self.el_replaces)
        self.el_replaces.currentIndexChanged.connect(self.load_cave_revision)
        self.el_encounters = QCheckBox('Wild encounters on the cave floor (behavior 0x08)')
        self.el_encounters.setChecked(True)
        form.addRow('', self.el_encounters)
        self.el_surf = QCheckBox('Surf water (behavior 0x15); unchecked = decorative, blocked')
        self.el_surf.setChecked(True)
        form.addRow('', self.el_surf)
        self.el_cell = QLineEdit(','.join(map(str, self.context['cell'])))
        form.addRow('Ambient cell', self.el_cell)
        self.el_remove = QLineEdit('')
        self.el_remove.setPlaceholderText('plate indices, e.g. 0,1,2   (blank = every plate that conflicts with rain)')
        form.addRow('Remove plates', self.el_remove)
        actions = QHBoxLayout()
        for text, slot in (('Inspect', self.inspect_elevation), ('Preview feature', self.stage_elevation)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            actions.addWidget(button)
        form.addRow('', actions)
        note = QLabel('Family canopy-coast-v1 (docs/TERRAIN_AUTHORING.md): one flat ground height, a terrace top of '
                      '1..12 tiles per side rising 16 model units, one-tile blocked rim with stock cliff faces and '
                      'corners, 3-wide stock stairs (00 06), ponds of 2..8 tiles with stock shore. Shapes add concave '
                      'outlines (inner corners), a second level, one-way ledges and river bends (water at least 2 '
                      'wide). Cells need the donor materials; seams, overlaps, existing stair landings, stranding and '
                      'budgets refuse before anything is written. Cave rooms (family cave-d41-v1) carve floors, '
                      'stepped rock walls and south-wall exits into empty space of a cave area created from the '
                      'stock D41 room; link exits with Connections (same-area holes allowed).')
        note.setWordWrap(True)
        form.addRow(note)
        self.el_action.currentIndexChanged.connect(self.elevation_fields)
        self.elevation_fields()
        return panel

    def elevation_fields(self, *_):
        action = self.el_action.currentData()
        rect = action in ('terrace', 'pond', 'waterfall')
        shaped = action in ('terrace_shape', 'water_shape', 'cave_room')
        for widget, on in ((self.el_x, rect), (self.el_z, rect), (self.el_w, rect),
                           (self.el_h, action in ('terrace', 'pond')),
                           (self.el_upper, action == 'waterfall'), (self.el_lower, action == 'waterfall'),
                           (self.el_access, action == 'terrace'), (self.el_rects, shaped),
                           (self.el_level, action == 'terrace_shape'), (self.el_shape_access, action == 'terrace_shape'),
                           (self.el_ledges, action == 'terrace_shape'), (self.el_climbs, action == 'terrace_shape'),
                           (self.el_surf, action in ('pond', 'water_shape')),
                           (self.el_cell, action == 'ambient'), (self.el_remove, action == 'ambient'),
                           (self.el_exits, action == 'cave_room'), (self.el_encounters, action == 'cave_room'),
                           (self.el_replaces, action in ('cave_room', 'waterfall'))):
            self.el_form.setRowVisible(widget, on)
        if action in ('cave_room', 'waterfall') and getattr(self, '_replaces_for', None) != action:
            self._replaces_for = action
            self.el_replaces.blockSignals(True)
            while self.el_replaces.count() > 1:
                self.el_replaces.removeItem(1)
            self.el_replaces.setCurrentIndex(0)
            self.el_replaces.blockSignals(False)
            try:
                view = self.project.terrain_view(self.ctx['header']['id'])
            except EditorError:
                return
            for f in view['features']:
                if f['spec']['action'] == action:
                    self.el_replaces.addItem(f"Revise {f['id']}", f)

    def load_cave_revision(self, *_):
        f = self.el_replaces.currentData()
        if not f:
            return
        spec = f['spec']
        if spec['action'] == 'waterfall':
            for widget, key in ((self.el_x, 'x'), (self.el_z, 'z'), (self.el_w, 'width'), (self.el_upper, 'upper'),
                                (self.el_lower, 'lower')):
                widget.setValue(spec[key])
            return
        self.el_rects.setText('; '.join(','.join(map(str, r)) for r in spec['rects']))
        self.el_exits.setText('; '.join(f"{e['x']},{e['z']}" for e in spec['exits']))
        self.el_encounters.setChecked(spec['encounters'])

    def inspect_elevation(self):
        try:
            view = self.project.terrain_view(self.ctx['header']['id'])
        except EditorError as exc:
            return self.fail(f'{exc.code}: {exc}')
        lines = [f"Family {view['family']} · weather {view['weather']} · rain conflicts {view['rain_plate_conflicts']}"]
        for f in view['features']:
            lines.append(f"  {f['id']}: {json.dumps(f['spec'])}")
        for c in view['cells']:
            lines.append(f"Cell {c['cell']} (map {c['map_member']}): terrace materials {c['terrace_materials']}, "
                         f"pond materials {c['pond_materials']}, features {c['features']}")
            for plate in c['sound_plates']:
                lines.append(f"  plate {plate['index']} {plate['sound']} at {plate['global']}"
                             + (' — conflicts with rain' if plate['rain_conflict'] else ''))
        self.summary.setPlainText('\n'.join(lines))
        self.status.setText('Inspect only; nothing staged.')

    def elevation_request(self):
        action = self.el_action.currentData()
        request = {'action': action}
        if action == 'ambient':
            cell = [int(v) for v in self.el_cell.text().split(',')]
            text = self.el_remove.text().strip()
            if text:
                remove = [int(v) for v in text.replace(' ', '').split(',') if v]
            else:
                view = self.project.terrain_view(self.ctx['header']['id'])
                row = next((c for c in view['cells'] if c['cell'] == cell), {'sound_plates': []})
                remove = [p['index'] for p in row['sound_plates'] if p['rain_conflict']]
            request.update(cell=cell, remove=remove)
        elif action in ('terrace_shape', 'water_shape', 'cave_room'):
            def parts(text):
                return [p for p in text.replace(' ', '').split(';') if p]
            request['rects'] = [[int(v) for v in part.split(',')] for part in parts(self.el_rects.text())]
            if action == 'cave_room':
                request['exits'] = [dict(zip(('x', 'z'), (int(v) for v in part.split(',')))) for part in parts(self.el_exits.text())]
                request['encounters'] = self.el_encounters.isChecked()
                if self.el_replaces.currentData():
                    request['replaces'] = self.el_replaces.currentData()['id']
            elif action == 'water_shape':
                request['traversable'] = self.el_surf.isChecked()
            else:
                request['level'] = self.el_level.value()
                request['access'] = []
                for part in parts(self.el_shape_access.text()):
                    side, _, xz = part.partition(':')
                    x, z = (int(v) for v in xz.split(','))
                    request['access'].append({'side': side, 'x': x, 'z': z})
                request['ledges'] = []
                for part in parts(self.el_ledges.text()):
                    side, _, rest = part.partition(':')
                    x, z, length = (int(v) for v in rest.split(','))
                    request['ledges'].append({'side': side, 'x': x, 'z': z, 'length': length})
                climbs = []
                for part in parts(self.el_climbs.text()):
                    side, _, xz = part.partition(':')
                    x, z = (int(v) for v in xz.split(','))
                    climbs.append({'side': side, 'x': x, 'z': z})
                if climbs:
                    request['climbs'] = climbs
        elif action == 'waterfall':
            request.update(x=self.el_x.value(), z=self.el_z.value(), width=self.el_w.value(),
                           upper=self.el_upper.value(), lower=self.el_lower.value())
            if self.el_replaces.currentData():
                request['replaces'] = self.el_replaces.currentData()['id']
        else:
            request.update(x=self.el_x.value(), z=self.el_z.value(), width=self.el_w.value(), height=self.el_h.value())
            if action == 'terrace':
                access = []
                for part in self.el_access.text().replace(' ', '').split(';'):
                    if part:
                        side, _, offset = part.partition(':')
                        access.append({'side': side, 'offset': int(offset or 0)})
                request['access'] = access
            else:
                request['traversable'] = self.el_surf.isChecked()
        return {'kind': 'elevation', 'context': self.context, 'request': request}

    def stage_elevation(self):
        try:
            operation = self.elevation_request()
        except ValueError:
            return self.fail('Stairs use side:offset (shapes: side:x,z); ledges side:x,z,length; rectangles x,z,w,h')
        self.stage([operation], f"Elevation: {operation['request']['action']}")

    def identity_panel(self):
        panel = QWidget()
        form = QFormLayout(panel)
        self.id_area = QLabel('')
        self.id_area.setWordWrap(True)
        form.addRow('Area', self.id_area)
        self.id_name = QLineEdit('')
        self.id_name.setPlaceholderText('in-game location name, up to 16 characters')
        form.addRow('Location name', self.id_name)
        self.id_share = QCheckBox("Use the parent's location name")
        form.addRow('', self.id_share)
        self.id_popup = QComboBox()
        form.addRow('Arrival popup', self.id_popup)
        self.id_day, self.id_night = QComboBox(), QComboBox()
        form.addRow('Day music', self.id_day)
        form.addRow('Night music', self.id_night)
        self.id_weather = QComboBox()
        form.addRow('Weather', self.id_weather)
        self.id_region = QComboBox()
        for text, value in (('From the Pokégear position', None), ('Johto', 'johto'), ('Kanto', 'kanto')):
            self.id_region.addItem(text, value)
        form.addRow('Region', self.id_region)
        self.id_marker = QComboBox()
        for text, value in (('Unchanged', None), ('Set position', 'set'), ('Clear (follow the spawn warp)', 'clear')):
            self.id_marker.addItem(text, value)
        form.addRow('Pokégear marker', self.id_marker)
        where = QHBoxLayout()
        self.id_x, self.id_y = spin(1, 45, 20), spin(2, 17, 13)
        for label, widget in (('x', self.id_x), ('y', self.id_y)):
            where.addWidget(QLabel(label))
            where.addWidget(widget)
        form.addRow('Marker cell', where)
        self.id_description = QPlainTextEdit('')
        self.id_description.setPlaceholderText('optional Pokégear description, two lines of up to 28 characters')
        self.id_description.setMaximumHeight(56)
        form.addRow('Description', self.id_description)
        self.id_parent = QComboBox()
        form.addRow('Parent (interiors)', self.id_parent)
        actions = QHBoxLayout()
        for text, slot in (('Preview identity', self.stage_identity), ('Static forest cells (auto)', self.stage_static),
                           ('Decorative trees', self.stage_trees)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            actions.addWidget(button)
        form.addRow('', actions)
        self.id_note = QLabel('')
        self.id_note.setWordWrap(True)
        form.addRow(self.id_note)
        return panel

    def current_identity(self):
        view = self.project.world_identity()
        row = next((r for r in view['areas'] if r['header'] == self.ctx['header']['id']), None)
        return view, row

    def refresh_identity(self):
        view, row = self.current_identity()
        values = view['valid_values']
        self.id_popup.clear()
        for value in values['popup']:
            self.id_popup.addItem('No popup' if value == 0 else f'Style {value}', value)
        for box in (self.id_day, self.id_night):
            box.clear()
            for value in values['music']:
                box.addItem(str(value), value)
        self.id_weather.clear()
        for value, name in values['weather'].items():
            self.id_weather.addItem(f'{value}: {name}', int(value))
        self.id_parent.clear()
        self.id_parent.addItem('Unchanged', None)
        self.id_parent.addItem('No parent', False)
        for other in view['areas']:
            if other['location_type'] in (1, 2) and (row is None or other['area'] != row['area']):
                self.id_parent.addItem(f"{other['project_name']} ({other['area']})", other['area'])
        cap = view['capacity']
        self.id_note.setText(
            f"Location names {cap['location_names']['used']}/{cap['location_names']['limit']} · Pokégear locations "
            f"{cap['town_map_locations']['used']}/{cap['town_map_locations']['limit']} · Headbutt tiles left: "
            + (', '.join(f'{k} {v}' for k, v in view['headbutt_tiles'].items()) or 'none')
            + '. Interiors never show the arrival popup (stock rule); a parented interior takes the parent\'s '
              'marker and region. Retained Headbutt trees are refused: created headers have no qualified table.')
        if row is None:
            self.id_area.setText(f"{self.ctx['name']} is not a created area; identity edits apply to created areas only.")
            self.identity_key = None
            return
        self.identity_key = row['area']
        self.id_area.setText(f"{row['project_name']} · {row['area']} · header {row['header']} · internal "
                             f"{row['internal_name']} · shows “{row['location_name']}” (section {row['map_section']})"
                             + (f" · parent {row['parent']}" if row['parent'] else ''))
        self.id_name.setText(row['location_name'] if row['own_section'] else '')
        self.id_share.setChecked(row['share_name'])
        for box, value in ((self.id_popup, row['popup']), (self.id_day, row['music']['day']),
                           (self.id_night, row['music']['night']), (self.id_weather, row['weather'])):
            if box.findData(value) >= 0:
                box.setCurrentIndex(box.findData(value))
        if row['town_map']:
            self.id_x.setValue(row['town_map']['x'])
            self.id_y.setValue(row['town_map']['y'])

    def identity_request(self):
        request = {'action': 'identity', 'area': self.identity_key,
                   'popup': self.id_popup.currentData(), 'weather': self.id_weather.currentData(),
                   'music': {'day': self.id_day.currentData(), 'night': self.id_night.currentData()}}
        parent = self.id_parent.currentData()
        if parent is not None:
            request['parent'] = parent
        if self.id_share.isChecked():
            request['share_name'] = True
        elif self.id_name.text().strip():
            request['name'] = self.id_name.text().strip()
        if parent in (None, False):
            if self.id_region.currentData():
                request['region'] = self.id_region.currentData()
            marker = self.id_marker.currentData()
            if marker == 'clear':
                request['town_map'] = False
            elif marker == 'set':
                request['town_map'] = {'x': self.id_x.value(), 'y': self.id_y.value()}
                text = self.id_description.toPlainText().strip()
                if text:
                    request['town_map']['description'] = text
        return {'kind': 'world', 'context': self.context, 'request': request}

    def stage_identity(self):
        if not self.identity_key:
            return self.fail('Open a created area to edit its identity')
        self.stage([self.identity_request()], f'Identity: {self.identity_key}')

    def stage_static(self):
        if not self.identity_key:
            return self.fail('Open a created area first')
        self.stage([{'kind': 'world', 'context': self.context,
                     'request': {'action': 'animation', 'area': self.identity_key, 'static': 'auto'}}],
                   f'Static forest cells: {self.identity_key}')

    def stage_trees(self):
        if not self.identity_key:
            return self.fail('Open a created area first')
        self.stage([{'kind': 'world', 'context': self.context,
                     'request': {'action': 'trees', 'area': self.identity_key, 'policy': 'decorative'}}],
                   f'Decorative trees: {self.identity_key}')

    # ---- state -----------------------------------------------------------------

    def refresh(self):
        if hasattr(self, 'travel_list'):
            self.refresh_travel()
        if hasattr(self, 'effect_list'):
            self.refresh_effects()
        if hasattr(self, 'group_list'):
            self.refresh_groups()
        if hasattr(self, 'env_list'):
            self.refresh_environments()
        view = self.project.world_areas()
        cap = view['capacity']
        self.capacity.setText(f"Revision {view['revision']} · created headers {cap['created_headers']}/{cap['limit']} · "
                              f"next header {cap['next_header']} · current area {self.ctx['name']} ({self.ctx['id']})")
        self.areas.clear()
        for area in view['areas']:
            owners = area['owners']
            item = QListWidgetItem(f"{area['name']} · header {area['header']} · {area['size'][0]}×{area['size'][1]} · "
                                   f"events {owners['events']} scripts {owners['scripts']}/{owners['level_script']} "
                                   f"text {owners['text']} encounters {owners['encounters']}")
            item.setData(Qt.ItemDataRole.UserRole, (area['header'], area['cells'][0]['cell']))
            self.areas.addItem(item)
        self.here.setText(f"Warps in {self.ctx['name']} ({self.ctx['id']}) and where they lead:")
        self.links.clear()
        for event in self.project.map_events(**self.context)['events']:
            if event['kind'] != 'warp':
                continue
            link = event.get('connection', {})
            where = (f"→ {link['name']} (header {link['header']}) cell {link['cell']}"
                     + (' · returns here' if link.get('returns_to_source') else '')) if link.get('resolved') \
                else f"→ unresolved: {link.get('reason', '')}"
            item = QListWidgetItem(f"warp {event['id']} at {event['x']},{event['z']} {where}")
            item.setData(Qt.ItemDataRole.UserRole, (link.get('header'), link.get('cell')))
            self.links.addItem(item)
        # Connections that touch this area can be moved (both ends stay paired).
        self.move_connection.clear()
        self.move_connection.addItem('Choose a connection', None)
        here = self.ctx['header']['id']
        for c in self.project.composed().get('world', {}).get('connections', []):
            if any(w['header'] == here for w in c['warps']):
                ends = '; '.join(f"{w['header']}:{w['x']},{w['z']}" for w in c['warps'])
                self.move_connection.addItem(f"{c['label']} ({ends})", c['index'])
        current = self.material.currentData()
        self.material.clear()
        self.material.addItem('Unchanged', None)
        for entry in self.project.surface_palette(**self.context):
            label = entry['material'] + (' (tall grass overlay)' if entry.get('overlay') else '')
            self.material.addItem(label, entry['material'])
        if current is not None and self.material.findData(current) >= 0:
            self.material.setCurrentIndex(self.material.findData(current))
        self.refresh_identity()

    def set_layout_cell(self, col, row, text):
        self.layout_table.setItem(row, col, QTableWidgetItem(text))

    def fill_window(self):
        grid = self.project.matrix_data(self.window_matrix.value())
        self.layout_table.clearContents()
        for row in range(self.window_h.value()):
            for col in range(self.window_w.value()):
                x, y = self.window_x.value() + col, self.window_y.value() + row
                if not (0 <= x < grid['width'] and 0 <= y < grid['height']):
                    continue
                header = grid['headers'][y][x] if grid['has_headers'] else self.template.value()
                self.set_layout_cell(col, row, f'{header}:{x},{y}')

    def layout_cells(self):
        cells = []
        for row in range(wa.MAX_SIDE):
            for col in range(wa.MAX_SIDE):
                item = self.layout_table.item(row, col)
                text = item.text().strip() if item else ''
                if not text:
                    continue
                header, _, at = text.partition(':')
                cells.append({'cell': [col, row], 'source': {'header': int(header),
                                                            'cell': [int(v) for v in at.split(',')]}})
        return cells

    def review_template(self):
        try:
            result = wa.review(self.project, self.area_request()['request'])
        except (EditorError, ValueError) as exc:
            return self.fail(str(exc))
        lines = [f"Template review · {result['cells']} cell(s)",
                 f"internal dependencies missing: {len(result['internal_missing'])}",
                 f"foreign overhangs: {len(result['foreign_overhangs'])}",
                 f"boundary losses (visible only if a reachable view reaches the outer ring): "
                 f"{len(result['boundary_losses'])}"]
        for row in result['internal_missing'][:6]:
            lines.append(f"  missing at {row['position']} from {row['from']}: {', '.join(row['materials'])}")
        for row in result['foreign_overhangs'][:6]:
            lines.append(f"  foreign at {row['position']} from {row['from']}: {', '.join(row['materials'])}")
        lines.append('altitudes: ' + ' '.join(f'{k}={v}' for k, v in result['altitudes'].items()))
        self.summary.setPlainText('\n'.join(lines))
        self.status.setText('Review only; nothing is planned or saved.')

    def stage_border(self):
        try:
            request = {'family': self.border_family.currentData(), 'tiles': rects(self.border_tiles.text())}
            if self.border_window.text().strip():
                x, z, w, h = (int(v) for v in self.border_window.text().split(','))
                request['window'] = {'x': x, 'z': z, 'width': w, 'height': h}
        except ValueError:
            return self.fail('Tiles and window use x,z,w,h rectangles')
        if self.border_material.currentData():
            request['material'] = self.border_material.currentData()
        if self.border_erase.isChecked():
            request['erase'] = True
        if self.border_decorative.isChecked() and request['family'] == 'tall_grass':
            request['behavior'] = 'ground'
        self.stage([{'kind': 'border', 'context': self.context, 'request': request}],
                   'Border path' if request['family'] == 'path' else 'Border tall grass')

    def area_request(self):
        try:
            cells = self.layout_cells()
        except ValueError:
            cells = []
        worldmap = [int(v) for v in self.worldmap.text().split(',')] if self.worldmap.text().strip() else None
        request = {'action': 'create', 'identity': self.identity.text().strip(), 'name': self.area_name.text().strip(),
                   'internal_name': self.internal_name.text().strip(), 'template_header': self.template.value(),
                   'cells': cells, 'encounters': self.encounters.currentData(), 'worldmap': worldmap,
                   'close': self.close_edges.isChecked()}
        if self.static_cells.isChecked():
            request['static'] = 'auto'
        return {'kind': 'world', 'context': cells[0]['source'] if cells else self.context, 'request': request}

    def stage_area(self):
        self.stage([self.area_request()], 'Create area')

    def stage_connection(self):
        request = {'action': 'connect', 'x': self.source_x.value(), 'z': self.source_z.value(),
                   'destination': {'header': self.destination.value()},
                   'arrival': {'x': self.arrival_x.value(), 'z': self.arrival_z.value()}}
        try:
            extra, landing = pairs(self.extra.text()), pairs(self.arrival_extra.text())
        except ValueError:
            return self.fail('Extra tiles use x,z; x,z')
        if extra:
            request['extra'] = extra
        if landing:
            request['arrival_extra'] = landing
        self.stage([{'kind': 'world', 'context': self.context, 'request': request}], 'Connect areas')

    def stage_move(self):
        if self.move_connection.currentData() is None:
            return self.fail('Choose a connection to move')
        request = {'action': 'move', 'connection': self.move_connection.currentData()}
        try:
            for key, widget in (('source', self.move_source), ('target', self.move_target)):
                if widget.text().strip():
                    request[key] = pairs(widget.text())
        except ValueError:
            return self.fail('Tiles use x,z; x,z')
        self.stage([{'kind': 'world', 'context': self.context, 'request': request}], 'Move entrance')

    def terrain_request(self):
        request = {}
        if self.use_selection.isChecked():
            tiles = sorted(getattr(self.inspector, 'selected_cells', ()) or ())
            request['tiles'] = [{'x': x, 'z': z} for x, z in tiles]  # global tiles
        else:
            request.update(x=self.t_x.value(), z=self.t_z.value(), width=self.t_w.value(), height=self.t_h.value())
        if self.stamp.isChecked():
            request['copy_from'] = {'header': self.stamp_header.value(),
                                    'cell': [int(v) for v in self.stamp_cell.text().split(',')],
                                    'x': self.stamp_x.value(), 'z': self.stamp_z.value()}
            return {'kind': 'terrain', 'context': self.context, 'request': request}
        for key, value in (('material', self.material.currentData()), ('ground', self.ground.currentData()),
                           ('blocked', self.blocked.currentData())):
            if value is not None:
                request[key] = value
        return {'kind': 'terrain', 'context': self.context, 'request': request}

    def stage_terrain(self):
        self.stage([self.terrain_request()], 'Terrain edit')

    def stage(self, operations, label):
        try:
            plan = self.project.plan_area_edit(copy.deepcopy(operations), label=label)
        except EditorError as exc:
            return self.fail(f'{exc.code}: {exc}')
        self.operations, self.plan, self.label = operations, plan, label
        lines = []
        for change in plan['preview']:
            if change['operation'] == 'world.area':
                a = change['allocation']
                lines.append(f"New area {change['identity']} · {len(change['cells'])} cell(s) → header {a['header']} "
                             f"({change['header']['internal_name']}), "
                             f"matrix {a['matrix']}, maps {a['maps']}, events {a['events']}, scripts {a['scripts']}, "
                             f"map-load {a['level_script']}, text {a['text']}, encounters {a['wild']}")
                for cell in change['cells']:
                    lines.append(f"  cell {cell['cell']} ← {cell['source']['id']} (map {cell['map_member']}, "
                                 f"{cell['closed']} edge tiles blocked"
                                 + (', static: no area texture animation' if cell.get('static') else '') + ')')
            elif change['operation'] == 'world.identity':
                ident = change['identity']
                marker = ident['town_map'] or ('parent' if ident['parent'] else 'spawn warp')
                lines.append(f"Identity {change['area']} (header {change['header']}): name "
                             f"{ident['name'] or '(parent/inherited)'} · section {ident['section']} · popup "
                             f"{ident['popup']} · music {ident['music']['day']}/{ident['music']['night']} · weather "
                             f"{ident['weather']} · {ident['region']} · marker {marker}"
                             + (f" · parent {ident['parent']}" if ident['parent'] else ''))
                for field, (old, new) in change['changes'].items():
                    lines.append(f"  {field}: {old} → {new}")
            elif change['operation'] == 'world.animation':
                lines.append(f"Static cells in {change['area']}: {change['static_cells']} (map members {change['members']})")
            elif change['operation'] == 'terrain.feature':
                r = change['report']
                lines.append(f"{change['feature']} in {change['context']['id']} ({r['family']}): ground {r['ground_height']}, "
                             f"cells {r['cells']}, polygons {change['model_counts']['before'][1]}→"
                             f"{change['model_counts']['after'][1]}, height table {change['bdhc_bytes'][0]}→"
                             f"{change['bdhc_bytes'][1]} bytes")
                if r['action'] == 'cave_room':
                    lines.append(f"  {r['floor_tiles']} floor / {r['wall_tiles']} wall tiles, corners convex "
                                 f"{r['convex_corners']} / concave {r['concave_corners']}, {r['encounters']}, exits "
                                 + (', '.join(str(e['tile']) for e in r['exits']) or 'none') + f" · {r['connection']}")
                elif r['action'] == 'waterfall':
                    lines.append(f"  fall {r['fall']} ({r['fall_behavior']}); water {r['lower_water_height']}→"
                                 f"{r['upper_water_height']}, Surf entries {r['surf_entries']}, upper reachable via the "
                                 f"fall: {r['upper_reachable_via_fall']}")
                elif r['action'] in ('terrace', 'terrace_shape'):
                    lines.append(f"  top {r['top_height']}, rim {r['rim_tiles']} blocked tile(s), stairs "
                                 + ', '.join(f"{s['side']} {s['tiles'][0]}..{s['tiles'][-1]}" for s in r['stairs'])
                                 + f", top reachable: {r['top_reachable']}, seams {r['seams'] or 'none'}")
                    if r['action'] == 'terrace_shape':
                        lines.append(f"  level {r['level']}{' on ' + r['base'] if r['base'] else ''}, corners outer "
                                     f"{r['outer_corners']} / inner {r['inner_corners']}, ledges "
                                     + (', '.join(f"{e['side']} {e['tiles'][0]}..{e['tiles'][-1]} (jump {e['jump']:#04x})"
                                                  for e in r['ledges']) or 'none')
                                     + (f", ledge landings reachable: {r['ledges_reachable']}" if r['ledges'] else ''))
                        for c in r.get('climbs', []):
                            lines.append(f"  Rock Climb {c['side']} at {c['tile']} ({c['behavior']}), landing "
                                         f"{c['landing']} reachable: {c['landing_reachable']}")
                else:
                    lines.append(f"  {r['water']}, water height {r['water_height']}, Surf entries {r['surf_entries']}")
                    if r['action'] == 'water_shape':
                        lines.append(f"  shore edges {r['shore_edges']}, corner ends convex {r['convex_corner_ends']} / "
                                     f"concave {r['concave_corner_ends']}, Surf reachable: {r['surf_reachable']}")
            elif change['operation'] == 'terrain.ambient':
                lines.append(f"Sound plates in {change['context']['id']}: {change['plates'][0]}→{change['plates'][1]}; removed "
                             + ', '.join(f"{p['sound']}@{p['global'][:2]}" for p in change['removed']))
            elif change['operation'] == 'world.entrance-move':
                for m in change['moves']:
                    lines.append(f"  header {m['header']} warp {m['id']}: {m['from']} → {m['to']} (still paired)")
            elif change['operation'] == 'world.connection':
                for w in change['warps']:
                    lines.append(f"  {w['kind']} {w['header']}:{w['id']} at {w['x']},{w['z']} → header "
                                 f"{w['destination']} warp {w['destination_warp']}")
            elif change['operation'] == 'surface.transaction' and change.get('family'):
                lines.append(f"{label} in {change['context']['id']}: {len(change['cells'])} ground tile(s), "
                             f"{change['pieces']} grass piece(s)")
            elif change['operation'] == 'surface.transaction':
                lines.append(f"Visible ground: {len(change['cells'])} tile(s) in {change['context']['id']}")
            elif change['operation'] == 'ground.transaction':
                lines.append(f"Ground material {change['action']} {change['request']['material']}: {change['effect']}")
                for user in change['impact']:
                    lines.append(f"  shared user: map {user['map_member']} " +
                                 ', '.join(f'{k} {v}' for k, v in user.items() if k != 'map_member'))
            elif change['operation'] == 'decal.transaction':
                lines.append(f"Decal {change['action']} {change['key']} in {change['context']['id']}: "
                             f"{change['before']} → {change['after']}")
            elif change['operation'] == 'prop.transaction':
                req = change.get('request') or {}
                where = f" at {req['x']},{req['z']}" if 'x' in req else ''
                lines.append(f"Prop {change.get('action')} {req.get('instance') or req.get('asset', '')}{where}")
            elif change['operation'] == 'map.group':
                r = change['report'] or {}
                lines.append(f"{change['label']}" + (f": copied {r.get('copied')} · shared "
                                                     f"{r.get('shared', []) + r.get('shared_assets', [])}" if r else ''))
            elif change['operation'] == 'effect.petals':
                r = change['report']
                lines.append(f"{change['label']}: {change['before']} → {change['after']}")
                if r:
                    lines.append(f"  ~{r['on_screen']} petals on screen, {r['speed_px_per_frame']} px/frame, "
                                 f"{r['seconds_to_cross']} s to cross; replaces weather {r['replaces_weather']}")
            elif change['operation'] == 'travel.transaction':
                after = change['after'] or {}
                lines.append(f"{change['label']}: spawn {after.get('spawn')}"
                             + (f", Fly flag {after['flag']:#x}" if after.get('flag') is not None else '')
                             + (', retired' if after.get('retired') else ''))
            elif change['operation'] == 'field.transaction':
                lines.append(f"{change['label']}: {change['family']} at {change['tile'][0]},{change['tile'][1]} · "
                             f"{change['crossings']} crossing(s)")
            else:
                lines.append(f"{change['operation']}: {change.get('label', '')} "
                             f"{len(change.get('permission_cells', []))} permission tile(s)")
        if plan['empty']:
            lines.append('No change.')
        self.summary.setPlainText('\n'.join(lines))
        self.apply_button.setEnabled(not plan['empty'])
        self.status.setText('Preview ready. Apply saves it as one undoable edit.')

    def fail(self, message):
        self.plan = None
        self.apply_button.setEnabled(False)
        self.status.setText(message)
        self.summary.setPlainText(message)

    def apply(self):
        if not self.plan:
            return
        try:
            result = self.project.apply_area_edit(self.project.doc['revision'], operations=copy.deepcopy(self.operations),
                                                  label=self.label)
        except EditorError as exc:
            return self.fail(f'{exc.code}: {exc}')
        self.plan = None
        self.apply_button.setEnabled(False)
        self.status.setText(f"Saved revision {result['revision']}. Undo in the map editor restores it together.")
        self.ctx = self.project.context(**self.context)
        self.refresh()
        if hasattr(self.inspector, 'reload_cells'):
            self.inspector.guard(self.inspector.refresh)

    def open_area(self):
        item = self.areas.currentItem()
        if item:
            header, cell = item.data(Qt.ItemDataRole.UserRole)
            self.inspector.load_context(header, list(cell))
            self.context = dict(header=header, cell=list(cell))
            self.ctx = self.project.context(**self.context)
            self.refresh()

    def open_destination(self):
        item = self.links.currentItem()
        if item:
            header, cell = item.data(Qt.ItemDataRole.UserRole)
            if header is not None and cell is not None:
                self.inspector.load_context(header, list(cell))
