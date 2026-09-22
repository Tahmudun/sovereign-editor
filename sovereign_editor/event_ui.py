"""Event selection, staging and connection navigation for the native inspector."""
import math

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QBrush, QPen, QFont
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QFormLayout, QSpinBox, QComboBox, QCheckBox,
                               QPushButton, QScrollArea, QPlainTextEdit)

COLORS = {'npc': '#98c9ff', 'background': '#f1d486', 'warp': '#d6a7ff', 'trigger': '#fa9f99'}
NAMES = {'npc': 'NPC', 'background': 'Interaction', 'warp': 'Warp', 'trigger': 'Trigger'}
MARKS = {'npc': 'N', 'background': 'B', 'warp': 'W', 'trigger': 'T'}


class EventControls:
    def init_events(self):
        self.event_data = None
        self.selected_event = None
        self.event_loading = False
        self.event_target = None

    def event_panel(self):
        body = QWidget()
        layout = QVBoxLayout(body)
        title = QLabel('MAP EVENTS')
        title.setStyleSheet('font-size: 17px; font-weight: 600;')
        layout.addWidget(title)
        self.event_scope = QLabel('Select an event marker or a record below.')
        self.event_scope.setWordWrap(True)
        layout.addWidget(self.event_scope)
        self.event_list = QListWidget()
        self.event_list.setMinimumHeight(110)
        self.event_list.setMaximumHeight(145)
        self.event_list.setStyleSheet("QListWidget::item { padding: 7px 4px; }")
        self.event_list.currentItemChanged.connect(self.event_selected)
        layout.addWidget(self.event_list)
        self.event_identity = QLabel('No event selected')
        self.event_identity.setWordWrap(True)
        layout.addWidget(self.event_identity)
        form = QFormLayout()
        self.event_inputs = {}
        for field, label, limit in [('x', 'Tile X', 65535), ('z', 'Tile Z', 65535),
                                    ('facing', 'Initial facing', 3), ('range_x', 'Movement range X', 31),
                                    ('range_z', 'Movement range Z', 31),
                                    ('destination', 'Destination header', 65535),
                                    ('destination_warp', 'Destination warp', 65535)]:
            if field == 'facing':
                widget = QComboBox()
                for text, value in [('North', 0), ('South', 1), ('West', 2), ('East', 3)]:
                    widget.addItem(text, value)
                widget.currentIndexChanged.connect(self.event_edited)
            else:
                widget = QSpinBox()
                widget.setRange(0, limit)
                widget.setStyleSheet("QSpinBox { min-height: 20px; padding: 3px 7px; }")
                widget.valueChanged.connect(self.event_edited)
            self.event_inputs[field] = widget
            form.addRow(label, widget)
        self.event_form = form
        layout.addLayout(form)
        self.event_reciprocal = QCheckBox('Connect destination back to this warp')
        self.event_reciprocal.toggled.connect(self.event_edited)
        layout.addWidget(self.event_reciprocal)
        self.event_connection = QLabel('')
        self.event_connection.setWordWrap(True)
        layout.addWidget(self.event_connection)
        self.event_open_target = QPushButton('Inspect destination →')
        self.event_open_target.clicked.connect(self.open_event_destination)
        layout.addWidget(self.event_open_target)
        self.event_association = QLabel('')
        self.event_association.setWordWrap(True)
        self.event_association.setObjectName('muted')
        layout.addWidget(self.event_association)
        self.event_model_button = QPushButton('Select associated sign model')
        self.event_model_button.clicked.connect(self.select_event_model)
        layout.addWidget(self.event_model_button)
        buttons = QHBoxLayout()
        for name, handler in [('Preview', self.preview_event), ('Cancel', self.cancel_event), ('Apply', self.apply_event)]:
            button = QPushButton(name)
            button.clicked.connect(handler)
            buttons.addWidget(button)
            setattr(self, 'event_' + name.lower() + '_button', button)
        self.event_apply_button.setObjectName('primary')
        layout.addLayout(buttons)
        self.event_detail = QPlainTextEdit()
        self.event_detail.setReadOnly(True)
        self.event_detail.setMinimumHeight(110)
        layout.addWidget(self.event_detail)
        actions = QHBoxLayout()
        undo = QPushButton('Undo last edit')
        undo.clicked.connect(self.undo)
        actions.addWidget(undo)
        export = QPushButton('Export ROM…')
        export.clicked.connect(self.export_dialog)
        actions.addWidget(export)
        layout.addLayout(actions)
        self.event_revision = QLabel('')
        layout.addWidget(self.event_revision)
        note = QLabel('N = NPC · B = background interaction\nW = warp · T = trigger (read only)\nScripts may override NPC motion or visibility in game.')
        note.setWordWrap(True)
        note.setObjectName('muted')
        layout.addWidget(note)
        layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(body)
        return scroll

    def event_tab_changed(self, index):
        conflict = (index == 0 and self.event_pending()) or (index == 1 and
                   (self.scenery_action or self.staged_move() or self.staged_cells or self.align_sign.isChecked()))
        if conflict:
            self.inspector_tabs.blockSignals(True)
            self.inspector_tabs.setCurrentIndex(1 - index)
            self.inspector_tabs.blockSignals(False)
            self.notice('Apply or cancel the pending edit before switching editors.', True)

    def current_event(self):
        if not self.event_data or not self.selected_event:
            return None
        return next((e for e in self.event_data['events'] if (e['kind'], e['id']) == self.selected_event), None)

    def event_values(self):
        record = self.current_event()
        if not record:
            return {}
        return {field: (self.event_inputs[field].currentData() if field == 'facing' else self.event_inputs[field].value())
                for field in record['editable_fields']}

    def event_pending(self):
        record = self.current_event()
        return bool(record and (self.event_reciprocal.isChecked()
                    or any(record[k] != v for k, v in self.event_values().items())))

    def refresh_events(self):
        pending = self.event_values() if self.event_pending() and getattr(self, '_event_revision', None) == self.project.doc['revision'] else None
        reciprocal = self.event_reciprocal.isChecked()
        self._event_revision = self.project.doc['revision']
        self.event_data = self.guard(lambda: self.project.map_events(header=self.header, cell=self.cell))
        self.event_list.blockSignals(True)
        self.event_list.clear()
        for record in self.event_data['events'] if self.event_data else []:
            if not record['in_cell']:
                continue
            item = QListWidgetItem(f"{NAMES[record['kind']]} {record['id']} · {record['x']}, {record['z']}"
                                   + (' •' if record['changed'] else ''))
            item.setData(Qt.ItemDataRole.UserRole, (record['kind'], record['id']))
            self.event_list.addItem(item)
            if (record['kind'], record['id']) == self.selected_event:
                self.event_list.setCurrentItem(item)
        self.event_list.blockSignals(False)
        if self.event_data:
            v = self.event_data
            self.event_scope.setText(f"Event file {v['member']} · {self.event_list.count()} records in this cell\n"
                                     f"Scripts {v['script_file']} · text {v['text_archive']}"
                                     + (f"\nShared by headers {v['shared_headers']}" if len(v['shared_headers']) > 1 else ''))
        self.event_revision.setText(f"Revision {self.project.doc['revision']} · saved in project.json")
        self.populate_event()
        if pending and self.current_event():
            self.event_loading = True
            for field, value in pending.items():
                widget = self.event_inputs[field]
                if field == 'facing':
                    widget.setCurrentIndex(widget.findData(value))
                else:
                    widget.setValue(value)
            self.event_reciprocal.setChecked(reciprocal)
            self.event_loading = False
            self.event_edited()

    def event_selected(self, item, previous):
        if item and not self.select_event(*item.data(Qt.ItemDataRole.UserRole)):
            self.event_list.blockSignals(True)
            self.event_list.setCurrentItem(previous)
            self.event_list.blockSignals(False)

    def select_event(self, kind, event_id):
        if getattr(self, 'wf', None) and self.wf.actions:
            self.notice('Apply or cancel Layout before selecting an event.', True)
            return False
        if self.event_pending() and (kind, event_id) == self.selected_event:
            return True
        if self.event_pending() and (kind, event_id) != self.selected_event:
            self.notice('Apply or cancel the pending event edit before selecting another event.', True)
            return False
        if self.scenery_action or self.staged_move() or self.staged_cells or self.align_sign.isChecked():
            self.notice('Apply or cancel the pending scenery/collision edit before selecting an event.', True)
            return False
        self.event_detail.clear()
        self.selected_slot = None
        self.selected_event = (kind, event_id)
        self.inspector_tabs.setCurrentIndex(1)
        self.event_list.blockSignals(True)
        for row in range(self.event_list.count()):
            item = self.event_list.item(row)
            if tuple(item.data(Qt.ItemDataRole.UserRole)) == self.selected_event:
                self.event_list.setCurrentItem(item)
        self.event_list.blockSignals(False)
        self.populate_event()
        self.redraw()
        return True

    def populate_event(self):
        record = self.current_event()
        self.event_loading = True
        for field, widget in self.event_inputs.items():
            visible = bool(record and field in record['editable_fields'])
            self.event_form.setRowVisible(widget, visible)
            if visible:
                if field == 'facing':
                    if widget.findData(record[field]) < 0:
                        widget.addItem(f'Preserved ({record[field]})', record[field])
                    widget.setCurrentIndex(widget.findData(record[field]))
                else:
                    # Show preserved unusual ranges exactly; editing still validates 0..31.
                    widget.setRange(min(0, record[field]), max(31 if field.startswith('range_') else 65535, record[field]))
                    widget.setValue(record[field])
        warp = bool(record and record['kind'] == 'warp')
        self.event_reciprocal.setVisible(warp)
        self.event_reciprocal.setChecked(False)
        self.event_open_target.setVisible(warp)
        self.event_connection.setVisible(warp)
        self.event_target = record.get('connection') if warp else None
        self.show_event_connection(self.event_target)
        if record:
            extra = (f"Sprite {record['sprite']} · script {record['script']} · movement {record['movement']} · flag {record['flag']}"
                     if record['kind'] == 'npc' else
                     f"Script {record['script']} · kind {record.get('background_type', '')} · direction {record.get('direction', '—')}"
                     if record['kind'] == 'background' else f"Height {record.get('y', '—')} preserved")
            self.event_identity.setText(f"{NAMES[record['kind']]} {record['id']}\n{extra}")
            self.event_association.setText(record['association']['note'])
        else:
            self.event_identity.setText('Select an event marker or a record.')
            self.event_association.setText('')
        self.event_model_button.setVisible(bool(record and record['association']['type'] == 'identified-model'))
        self.event_preview_button.setEnabled(bool(record and record['editable_fields']))
        self.event_apply_button.setEnabled(False)
        self.event_cancel_button.setEnabled(False)
        self.event_loading = False

    def show_event_connection(self, connection):
        self.event_open_target.setEnabled(bool(connection and connection.get('resolved')))
        if not connection:
            self.event_connection.setText('')
        elif connection['resolved']:
            target = connection['event']
            self.event_connection.setText(f"→ {connection['name']} · header {connection['header']} · warp {target['id']}\n"
                                          f"Tile {target['x']}, {target['z']} · cell {connection['cell']}\n"
                                          + ('Returns to this source.' if connection['returns_to_source'] else
                                             f"Return → header {target['destination']} / warp {target['destination_warp']}"))
        else:
            self.event_connection.setText('Destination unresolved: ' + connection['reason'])

    def event_edited(self, *_):
        if self.event_loading:
            return
        pending = self.event_pending()
        self.event_apply_button.setEnabled(pending)
        self.event_cancel_button.setEnabled(pending)
        self.event_detail.setPlainText('Staged only. Preview checks the record and any destination before Apply.' if pending else '')
        if self.current_event() and self.current_event()['kind'] == 'warp':
            self.event_target = None
            self.event_open_target.setEnabled(False)
            self.event_connection.setText('Preview to inspect the staged connection.')
        self.redraw()

    def event_request(self):
        record = self.current_event()
        return dict(header=self.header, cell=self.cell, kind=record['kind'], event_id=record['id'],
                    values={k: v for k, v in self.event_values().items() if record[k] != v},
                    reciprocal=self.event_reciprocal.isChecked())

    def show_event_plan(self, preview, saved=False):
        lines = ['SAVED' if saved else 'PREVIEW · not saved']
        for change in preview['events']:
            fields = [f"{k}: {change['from'][k]} → {v}" for k, v in change['to'].items() if change['from'][k] != v]
            lines.append(f"{NAMES[change['kind']]} {change['id']} · header {change['header']}\n" + '\n'.join(fields))
        lines.append(preview['note'])
        self.event_detail.setPlainText('\n\n'.join(lines))
        if 'connection' in preview:
            self.event_target = preview['connection']
            self.show_event_connection(self.event_target)

    def preview_event(self):
        if not self.current_event():
            return
        plan = self.guard(lambda: self.project.plan_event_edit(**self.event_request()))
        if plan:
            self.show_event_plan(plan['preview'])
            self.notice('Event preview only. Nothing was saved.')

    def apply_event(self):
        if not self.current_event():
            return
        result = self.guard(lambda: self.project.apply_event_edit(self.project.doc['revision'], **self.event_request()))
        if result:
            self.refresh()
            self.show_event_plan(result['preview'], saved=result['changed'])
            self.notice(f"Event edit saved at revision {result['revision']}." if result['changed'] else 'No event bytes changed.')

    def cancel_event(self):
        self.populate_event()
        self.event_detail.clear()
        self.redraw()
        self.notice('Staged event edit discarded. Nothing was written.')

    def open_event_destination(self):
        target = self.event_target
        if not target or not target.get('resolved'):
            return
        if self.event_pending():
            self.notice('Apply or cancel the staged edit before navigating. Destination details are shown above.', True)
            return
        self.load_context(target['header'], target['cell'])
        self.select_event('warp', target['event']['id'])

    def select_event_model(self):
        if self.event_pending():
            self.notice('Apply or cancel the event edit before selecting its model.', True)
            return
        record = self.current_event()
        self.select_placement(record['association']['slot'])
        self.inspector_tabs.setCurrentIndex(0)

    def pick_event(self, x, z):
        if not self.events_toggle.isChecked() or not self.event_data:
            return False
        ox, oz = self.view_data['context']['origin']
        found = [e for e in self.event_data['events'] if e['in_cell'] and
                 e['x'] == math.floor(x + ox) and e['z'] == math.floor(z + oz)]
        if not found:
            return False
        # Overlapping records remain individually selectable from the list.
        self.select_event(found[0]['kind'], found[0]['id'])
        return True

    def draw_events(self, ox, oz):
        if not self.events_toggle.isChecked() or not self.event_data:
            return
        for record in self.event_data['events']:
            if not record['in_cell']:
                continue
            selected = (record['kind'], record['id']) == self.selected_event
            values = self.event_values() if selected else {}
            x, z = values.get('x', record['x']), values.get('z', record['z'])
            color = QColor(COLORS[record['kind']])
            pen = QPen(color, 3 if selected else 1.5)
            pen.setCosmetic(True)
            mark = self.graphics.addRect((x - ox) * 32 + 3, (z - oz) * 32 + 3, 26, 26,
                                         pen, QBrush(QColor(20, 27, 32, 220)))
            mark.setZValue(20)
            text = self.graphics.addText(MARKS[record['kind']] + str(record['id']))
            text.setFont(QFont('Arial', 9, QFont.Weight.Bold))
            text.setDefaultTextColor(color)
            text.setPos((x - ox) * 32 + 3, (z - oz) * 32 + 3)
            text.setZValue(21)
            mark.setToolTip(f"{NAMES[record['kind']]} {record['id']} · {x},{z}")
            if selected and (x, z) != (record['x'], record['z']):
                line = self.graphics.addLine((record['x'] - ox + .5) * 32, (record['z'] - oz + .5) * 32,
                                             (x - ox + .5) * 32, (z - oz + .5) * 32, QPen(color, 2, Qt.PenStyle.DashLine))
                line.setZValue(19)
            if selected and record['kind'] == 'npc':
                rx, rz = max(0, values.get('range_x', record['range_x'])), max(0, values.get('range_z', record['range_z']))
                box = self.graphics.addRect((x - rx - ox) * 32, (z - rz - oz) * 32,
                                            (rx * 2 + 1) * 32, (rz * 2 + 1) * 32,
                                            QPen(color, 2, Qt.PenStyle.DashLine), Qt.BrushStyle.NoBrush)
                box.setZValue(19)
