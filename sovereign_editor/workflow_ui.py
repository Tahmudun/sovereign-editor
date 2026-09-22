"""Direct map tools and editable action staging in the main inspector canvas."""
import copy
import math
from PySide6.QtCore import Qt, QRectF, QSize
from PySide6.QtGui import QColor, QPen, QBrush, QIcon, QPixmap, QShortcut, QKeySequence
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
    QPushButton, QComboBox, QSpinBox, QListWidget, QListWidgetItem, QCheckBox, QScrollArea,
    QDialog, QDialogButtonBox, QLineEdit, QAbstractItemView)
from .workflow_session import WorkflowSession
from .formats import EditorError, require


class WorkflowControls:
    def workflow_tab_changed(self, index):
        index = self.inspector_tabs.currentIndex()
        if index != 2 and self.wf.actions:
            self.inspector_tabs.blockSignals(True)
            self.inspector_tabs.setCurrentIndex(2)
            self.inspector_tabs.blockSignals(False)
            self.notice('Apply or cancel Layout before switching editors.', True)
            index = 2
        if index == 2 and (self.event_pending() or self.scenery_action or self.staged_move() or self.staged_cells):
            self.inspector_tabs.setCurrentIndex(1 if self.event_pending() else 0)
            self.notice('Apply or cancel the current edit before opening Layout.', True)
            return
        if index == 2:
            self.selected_slot = None; self.selected_event = None
            self.x_input.set_anchor(0); self.z_input.set_anchor(0)
            self.wf_sync()
        for b in self.tool_buttons.values():
            b.setVisible(index != 2)
        for b in self.wf_canvas_buttons:
            b.setVisible(index == 2)
        if index == 2:
            self.help_label.setText('  Shift-click / marquee: select · Drag: move group · Arrows: nudge · Space-drag: pan · ⌘Z / ⇧⌘Z: undo / redo')
        else:
            self.help_label.setText(f'  {self.HELP[self.tool]}  {self.PAN_HELP}')
        self.wf_gesture = None
        self.redraw()

    def init_workflow(self):
        self.wf = WorkflowSession(self.project)
        self.wf_ids = set()
        self.wf_gesture = None
        self.wf_hover = None
        self.wf_swatches_key = None
        self.wf_sample = None

    def workflow_panel(self):
        host = QWidget(); box = QVBoxLayout(host)
        title = QLabel('LAYOUT WORKSPACE'); title.setStyleSheet('font-weight:600; letter-spacing:1px;')
        box.addWidget(title)
        self.wf_tool = QComboBox()
        for text, value in [('Select / move group', 'select'), ('Paint surface', 'brush'),
                            ('Paint rectangle', 'rectangle'), ('Sample surface', 'sample'),
                            ('Select cells', 'cells'), ('Block cells', 'block'), ('Unblock cells', 'unblock')]:
            self.wf_tool.addItem(text, value)
        box.addWidget(self.wf_tool)
        self.wf_tool.currentIndexChanged.connect(lambda: self.wf_changed_tool())
        row = QHBoxLayout(); row.addWidget(QLabel('Brush size'))
        self.wf_size = QSpinBox(); self.wf_size.setRange(1, 8); row.addWidget(self.wf_size); box.addLayout(row)
        self.wf_material = QComboBox(); self.wf_material.setIconSize(QSize(36, 36)); box.addWidget(self.wf_material)
        self.wf_material.currentIndexChanged.connect(self.wf_change_material)
        self.wf_object_lock = QCheckBox('Lock object selection'); box.addWidget(self.wf_object_lock)
        self.wf_event_lock = QCheckBox('Lock event selection'); self.wf_event_lock.setChecked(True); box.addWidget(self.wf_event_lock)
        help_ = QLabel('Shift-click or marquee selects objects. Drag moves the group. Select cells to define collision and floor repair.')
        help_.setWordWrap(True); help_.setStyleSheet('color:#a6b5be;'); box.addWidget(help_)
        self.wf_selection_label = QLabel('No objects selected'); self.wf_selection_label.setWordWrap(True); box.addWidget(self.wf_selection_label)
        self.wf_move_cells = QCheckBox('Use selected collision cells')
        self.wf_move_cells.setToolTip('Move or copy the explicitly selected blocked cells along with unbound objects.')
        box.addWidget(self.wf_move_cells)
        row = QHBoxLayout()
        self.wf_button(row, 'Duplicate', self.wf_duplicate)
        self.wf_button(row, 'Clear selection', self.wf_clear_selection); box.addLayout(row)
        self.wf_groups = QComboBox(); self.wf_groups.addItem('Saved groups…', None); box.addWidget(self.wf_groups)
        self.wf_groups.activated.connect(self.wf_choose_group)
        row = QHBoxLayout()
        self.wf_button(row, 'Save links…', self.wf_bind)
        self.wf_button(row, 'Remove links', self.wf_unbind); box.addLayout(row)
        self.wf_room_button = self.wf_button(box, 'Make this interior independent', self.wf_isolate)
        self.wf_room_status = QLabel(''); self.wf_room_status.setWordWrap(True); box.addWidget(self.wf_room_status)
        box.addWidget(QLabel('PENDING ACTIONS'))
        self.wf_queue = QListWidget(); self.wf_queue.setMinimumHeight(85); self.wf_queue.setMaximumHeight(120); box.addWidget(self.wf_queue)
        self.wf_queue.itemDoubleClicked.connect(lambda *_: self.wf_guard(self.wf_edit))
        row = QHBoxLayout(); self.wf_button(row, 'Edit…', self.wf_edit); self.wf_button(row, 'Remove', self.wf_remove); box.addLayout(row)
        self.wf_before = QCheckBox('Show before'); self.wf_before.toggled.connect(lambda: self.refresh()); box.addWidget(self.wf_before)
        box.addStretch()
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(host)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        container = QWidget(); outer = QVBoxLayout(container); outer.setContentsMargins(0, 0, 0, 0); outer.addWidget(scroll, 1)
        row = QHBoxLayout()
        self.wf_undo_button = self.wf_button(row, 'Undo', self.wf_undo)
        self.wf_redo_button = self.wf_button(row, 'Redo', self.wf_redo); outer.addLayout(row)
        row = QHBoxLayout(); self.wf_cancel_button = self.wf_button(row, 'Cancel', self.wf_cancel)
        self.wf_apply_button = self.wf_button(row, 'Apply layout', self.wf_apply); self.wf_apply_button.setObjectName('primary'); outer.addLayout(row)
        self.wf_status = QLabel('Changes stay in preview until Apply.'); self.wf_status.setWordWrap(True)
        self.wf_status.setMinimumHeight(36); self.wf_status.setMaximumHeight(78); outer.addWidget(self.wf_status)
        for key, callback in [(QKeySequence.StandardKey.Undo, self.wf_undo), (QKeySequence.StandardKey.Redo, self.wf_redo)]:
            shortcut = QShortcut(QKeySequence(key), self.grid)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(lambda f=callback: self.wf_guard(f))
        return container

    def wf_button(self, layout, text, callback):
        b = QPushButton(text); b.clicked.connect(lambda: self.wf_guard(callback)); layout.addWidget(b); return b

    def wf_guard(self, callback):
        try:
            return callback()
        except (EditorError, ValueError, TypeError, OSError) as exc:
            self.wf_status.setText(str(exc)); self.notice(str(exc), True)
            return None

    def wf_active(self):
        return hasattr(self, 'inspector_tabs') and self.inspector_tabs.currentIndex() == 2

    def wf_changed_tool(self):
        self.wf_gesture = None
        if hasattr(self, 'grid'):
            self.grid.viewport().setCursor(Qt.CursorShape.ArrowCursor if self.wf_tool.currentData() == 'select' else Qt.CursorShape.CrossCursor)
        if hasattr(self, 'wf_status'):
            self.wf_status.setText('Paint on the map; each stroke is one pending action. 64 authored surface tiles per map.')

    def wf_change_material(self, *_):
        self.wf_sample = None
        self.redraw()

    def wf_ref(self):
        return {'header': self.header, 'cell': list(self.cell)}

    def wf_action(self, kind, request):
        return {'kind': kind, 'context': self.wf_ref(), 'request': request}

    def wf_stage(self, action):
        require(not self.wf_before.isChecked(), 'Turn off Show before to edit the preview')
        before_ids = {p['object_id'] for p in self.wf.preview.map_view(**self.wf_ref(), include_grid=False)['placements']}
        self.wf.stage(action)
        if action['kind'] == 'layout' and action['request'].get('action') == 'duplicate':
            self.wf_ids = {p['object_id'] for p in self.wf.preview.map_view(**self.wf_ref(), include_grid=False)['placements']} - before_ids
        self.wf_gesture = None
        self.refresh()
        self.wf_status.setText(f'{len(self.wf.actions)} action(s) in preview. Apply saves them as one history step.')

    def wf_sync(self):
        if not hasattr(self, 'wf_queue') or not self.view_data:
            return
        preview = self.wf.preview; ctx = preview.context(**self.wf_ref())
        key = (ctx['map_member'], ctx['map_sha256'])
        if key != self.wf_swatches_key:
            self.wf_sample = None
            self.wf_material.blockSignals(True); self.wf_material.clear()
            self.wf_material.addItem('Restore original surface', None)
            try:
                for entry in preview.surface_palette(**self.wf_ref(), images=True):
                    self.wf_material.addItem(QIcon(entry.get('image', '')), entry['texture'], entry['material'])
                if self.wf_material.count() > 1:
                    self.wf_material.setCurrentIndex(1)
            except EditorError as exc:
                self.wf_status.setText(str(exc))
            self.wf_material.blockSignals(False); self.wf_swatches_key = key
        existing = {p['object_id'] for p in preview.map_view(**self.wf_ref(), include_grid=False)['placements']}
        self.wf_ids.intersection_update(existing)
        self.wf_selection_label.setText(f'{len(self.wf_ids)} objects · {len(self.selected_cells)} explicit cells selected')
        old = self.wf_groups.currentData(); self.wf_groups.blockSignals(True); self.wf_groups.clear(); self.wf_groups.addItem('Saved groups…', None)
        for g in preview.linked_groups(**self.wf_ref()):
            self.wf_groups.addItem(g['name'], g['name'])
        self.wf_groups.setCurrentIndex(max(0, self.wf_groups.findData(old))); self.wf_groups.blockSignals(False)
        row = self.wf_queue.currentRow(); self.wf_queue.clear()
        for i, a in enumerate(self.wf.actions):
            r = a['request']; kind = a['kind']
            label = {'interior': 'Independent interior', 'group': f"{r.get('action', '').capitalize()} links: {r.get('name', '')}",
                     'paint': f"Paint {len(r.get('cells', []))} tiles", 'collision': f"{'Block' if r.get('blocked') else 'Unblock'} {len(r.get('cells', []))} tiles",
                     'layout': f"{r.get('action', 'move').capitalize()} {r.get('group') or str(len(r.get('object_ids', []))) + ' objects'} · {r.get('dx', 0):+d}, {r.get('dz', 0):+d}"}.get(kind, kind.capitalize())
            self.wf_queue.addItem(f'{i + 1}. {label}')
        self.wf_queue.setCurrentRow(min(max(row, 0), self.wf_queue.count() - 1))
        pending = bool(self.wf.actions)
        self.wf_apply_button.setEnabled(pending and not self.wf.plan['empty'])
        self.wf_cancel_button.setEnabled(pending or bool(self.wf.undone))
        self.wf_undo_button.setEnabled(pending or bool(self.project.doc['history']))
        self.wf_redo_button.setEnabled(bool(self.wf.undone) or not pending and bool(self.project.doc.get('redo')))
        independent = self.header in preview._room_headers
        self.wf_room_button.setEnabled(ctx['area_data']['area_type'] == 0 and not independent)
        self.wf_room_status.setText('Independent room: other houses keep their original map.' if independent else 'Shared rooms can be isolated before decorating.')
        for i in (0, 1):
            self.inspector_tabs.setTabEnabled(i, not pending)
        self.export_button.setEnabled(not pending)

    def wf_choose_group(self, *_):
        name = self.wf_groups.currentData()
        g = next((g for g in self.wf.preview.linked_groups(**self.wf_ref()) if g['name'] == name), None)
        if g:
            self.wf_ids = {o['id'] for o in g['objects']}; self.selected_cells = {(c['x'], c['z']) for c in g['cells']}
            self.wf_tool.setCurrentIndex(0); self.redraw(); self.wf_sync()

    def wf_selected_group(self):
        matches = [g for g in self.wf.preview.linked_groups(**self.wf_ref()) if self.wf_ids & {o['id'] for o in g['objects']}]
        require(len(matches) <= 1, 'Select one saved group at a time')
        if matches:
            ids = {o['id'] for o in matches[0]['objects']}
            require(self.wf_ids.issubset(ids), 'Move the saved group separately from unbound objects')
            self.wf_ids = ids
            return matches[0]

    def wf_layout(self, dx, dz, mode='move'):
        require(bool(self.wf_ids), 'Select objects on the map first')
        g = self.wf_selected_group()
        request = dict(action=mode, dx=dx, dz=dz, object_ids=sorted(self.wf_ids))
        if g:
            request['group'] = g['name']
        elif self.wf_move_cells.isChecked():
            request['cells'] = [{'x': x, 'z': z} for x, z in sorted(self.selected_cells)
                                if self.wf.preview.permission_cells(**self.wf_ref(), x=x, z=z)['cells'][0]['collision'] & 128]
        self.wf_stage(self.wf_action('layout', request))

    def wf_clear_selection(self):
        self.wf_ids.clear(); self.selected_cells.clear(); self.redraw(); self.wf_sync()

    def wf_isolate(self):
        self.wf_stage(self.wf_action('interior', {}))

    def wf_cancel(self):
        self.wf = WorkflowSession(self.project); self.wf_ids.clear(); self.wf_gesture = None
        self.selected_cells.clear()
        self.wf_before.setChecked(False); self.refresh(); self.wf_status.setText('Preview discarded; saved project unchanged.')

    def wf_apply(self):
        result = self.wf.apply(); self.wf_before.setChecked(False); self._clear_staged(); self.refresh()
        self.wf_status.setText(f"Layout saved at revision {result['revision']}. Undo restores the complete edit.")

    def wf_undo(self):
        if self.wf.actions:
            self.wf.undo()
        else:
            self.project.undo(self.project.doc['revision']); self.wf = WorkflowSession(self.project); self._clear_staged()
        self.refresh()

    def wf_redo(self):
        if self.wf.undone:
            self.wf.redo()
        else:
            require(not self.wf.actions, 'Apply or cancel pending actions before redoing a saved edit')
            self.project.redo(self.project.doc['revision']); self.wf = WorkflowSession(self.project); self._clear_staged()
        self.refresh()

    def wf_remove(self):
        self.wf.remove(self.wf_queue.currentRow()); self.refresh()

    def wf_offset_dialog(self, title, dx=0, dz=0):
        d = QDialog(self); d.setWindowTitle(title); layout = QVBoxLayout(d); form = QFormLayout(); layout.addLayout(form)
        x, z = QSpinBox(), QSpinBox()
        for widget, value, label in ((x, dx, 'Tiles east / west'), (z, dz, 'Tiles south / north')):
            widget.setRange(-31, 31); widget.setValue(value); form.addRow(label, widget)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(d.accept); buttons.rejected.connect(d.reject); layout.addWidget(buttons)
        return (x.value(), z.value()) if d.exec() else None

    def wf_duplicate(self):
        from PySide6.QtCore import QPointF
        require(bool(self.wf_ids), 'Select objects to duplicate')
        require(self.wf_selected_group() is None, 'Remove saved links before duplicating these objects', 'BOUND_OBJECT')
        self.wf_tool.setCurrentIndex(0)
        anchor = next(p['position'] for p in self.view_data['placements'] if p['object_id'] == sorted(self.wf_ids)[0])
        ox, oz = self.view_data['context']['origin']
        point = QPointF((anchor['x']-ox)*32, (anchor['z']-oz)*32)
        self.wf_gesture = {'kind': 'duplicate', 'start': point, 'end': point}
        self.wf_status.setText('Click the map to place a copy. Escape cancels. Selected event logic is never duplicated.')

    def wf_edit(self):
        index = self.wf_queue.currentRow(); require(0 <= index < len(self.wf.actions), 'Select a pending action')
        a = copy.deepcopy(self.wf.actions[index]); r = a['request']
        if a['kind'] == 'layout':
            offset = self.wf_offset_dialog('Adjust pending move', r.get('dx', 0), r.get('dz', 0))
            if not offset:
                return
            r.update(dx=offset[0], dz=offset[1])
        elif a['kind'] == 'paint':
            d = QDialog(self); d.setWindowTitle('Change stroke surface'); box = QVBoxLayout(d); choice = QComboBox()
            for i in range(self.wf_material.count()):
                choice.addItem(self.wf_material.itemIcon(i), self.wf_material.itemText(i), self.wf_material.itemData(i))
            choice.setCurrentIndex(max(0, choice.findData(r['material']))); box.addWidget(choice)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            buttons.accepted.connect(d.accept); buttons.rejected.connect(d.reject); box.addWidget(buttons)
            if not d.exec():
                return
            r['material'] = choice.currentData()
            r.pop('sample', None)
        elif a['kind'] == 'collision':
            r['blocked'] = not r['blocked']
        else:
            raise EditorError('INVALID_INPUT', 'Remove this pending action and create its replacement with the map controls.')
        self.wf.replace(index, a); self.refresh()

    def wf_bind(self):
        require(bool(self.wf_ids), 'Select the objects to bind first')
        existing = self.wf_selected_group()
        d = QDialog(self); d.setWindowTitle('Remember this group and its links'); d.resize(440, 520)
        box = QVBoxLayout(d); form = QFormLayout(); box.addLayout(form); name = QLineEdit(); form.addRow('Group name', name)
        name.setObjectName('group_name')
        if existing:
            name.setText(existing['name']); name.setReadOnly(True)
        note = QLabel(f'{len(self.wf_ids)} selected objects. Select exact cells on the map before opening this dialog. Only the checked links below will move with this group.')
        note.setWordWrap(True); box.addWidget(note)
        cells = [{'x': x, 'z': z} for x, z in sorted(self.selected_cells)]
        blocked = [c for c in cells if self.wf.preview.permission_cells(**self.wf_ref(), **c)['cells'][0]['collision'] & 128]
        replace = QCheckBox('Replace footprints with selected cells'); replace.setVisible(bool(existing)); box.addWidget(replace)
        replace.setObjectName('replace_footprints')
        collision = QCheckBox(); collision.setObjectName('group_collision'); box.addWidget(collision)
        repair = QCheckBox(); repair.setObjectName('group_repair'); box.addWidget(repair)
        def footprints():
            if existing and not replace.isChecked():
                return existing['cells'], (existing['repair'] or {}).get('cells', [])
            return blocked, cells
        def labels():
            collision_cells, repair_cells = footprints()
            collision.setText(f'Include {len(collision_cells)} blocked cells')
            repair.setText(f'Repair {len(repair_cells)} floor cells when vacated')
        labels(); replace.toggled.connect(labels)
        collision.setChecked(bool(existing['cells'] if existing else blocked))
        repair.setChecked(bool(existing and existing['repair']))
        material = QComboBox()
        for i in range(1, self.wf_material.count()):
            material.addItem(self.wf_material.itemIcon(i), self.wf_material.itemText(i), self.wf_material.itemData(i))
        saved_repair = existing and existing['repair']
        material.setCurrentIndex(max(0, material.findData(saved_repair['material'] if saved_repair else self.wf_material.currentData()))); box.addWidget(material)
        box.addWidget(QLabel('Explicit interaction / door links'))
        links = QListWidget(); links.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection); box.addWidget(links)
        for e in self.wf.preview.map_events(**self.wf_ref())['events']:
            if e['in_cell'] and e['editable_fields']:
                item = QListWidgetItem(f"{e['kind'].capitalize()} {e['id']} at {e['x']}, {e['z']}")
                link = {'kind': e['kind'], 'event_id': e['id']}
                item.setData(Qt.ItemDataRole.UserRole, link); links.addItem(item)
                item.setSelected(bool(existing and any(all(l[k] == v for k, v in link.items()) for l in existing['events'])))
        note = QLabel('Floor repair uses the chosen repeating surface. Check surrounding borders and shadows in the preview. Raised or baked geometry may refuse.')
        note.setWordWrap(True); box.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(d.accept); buttons.rejected.connect(d.reject); box.addWidget(buttons)
        if d.exec():
            collision_cells, repair_cells = footprints()
            repair_request = {'cells': repair_cells, 'material': material.currentData()} if repair.isChecked() else None
            if repair_request is not None:
                if saved_repair and not replace.isChecked() and saved_repair['material'] == material.currentData():
                    if 'sample' in saved_repair:
                        repair_request['sample'] = copy.deepcopy(saved_repair['sample'])
                elif self.wf_sample and self.wf_sample['material'] == material.currentData():
                    repair_request['sample'] = {k: self.wf_sample[k] for k in ('x','z')}
            self.wf_stage(self.wf_action('group', dict(action='save', name=name.text().strip(), object_ids=sorted(self.wf_ids),
                cells=collision_cells if collision.isChecked() else [], events=[i.data(Qt.ItemDataRole.UserRole) for i in links.selectedItems()],
                repair=repair_request)))

    def wf_unbind(self):
        name = self.wf_groups.currentData(); require(name, 'Choose a saved group')
        self.wf_stage(self.wf_action('group', {'action': 'remove', 'name': name}))

    def wf_pick(self, point):
        if self.wf_object_lock.isChecked() or not self.toggles['Placements'].isChecked():
            return None
        slot = self.pick_placement(point.x() / 32, point.y() / 32)
        return next((p['object_id'] for p in self.view_data['placements'] if p['slot'] == slot), None)

    def wf_press(self, point, modifiers):
        if not self.wf_active():
            return False
        if self.wf_before.isChecked():
            self.notice('Turn off Show before to edit the preview.', True); return True
        if self.wf_gesture and self.wf_gesture['kind'] == 'duplicate':
            g = self.wf_gesture; self.wf_gesture = None
            dx = round((point.x()-g['start'].x())/32); dz = round((point.y()-g['start'].y())/32)
            self.wf_guard(lambda: self.wf_layout(dx, dz, mode='duplicate'))
            return True
        tool = self.wf_tool.currentData(); ox, oz = self.view_data['context']['origin']
        tile = (math.floor(point.x() / 32) + ox, math.floor(point.y() / 32) + oz)
        if tool == 'sample':
            def sample():
                r = self.wf.preview.sample_surface(*tile, **self.wf_ref())
                self.wf_material.setCurrentIndex(self.wf_material.findData(r['material'])); self.wf_tool.setCurrentIndex(1)
                self.wf_sample = r
                self.wf_status.setText(f'Sampled floor at {tile[0]}, {tile[1]}; its native repeating pattern is retained.')
            self.wf_guard(sample); return True
        if tool == 'select':
            identity = self.wf_pick(point)
            additive = bool(modifiers & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier))
            if identity:
                if additive:
                    self.wf_ids ^= {identity}; self.redraw(); self.wf_sync(); return True
                if identity not in self.wf_ids:
                    self.wf_ids = {identity}
                self.wf_guard(self.wf_selected_group)
                self.wf_gesture = {'kind': 'drag', 'start': point, 'end': point}
            else:
                if not self.wf_event_lock.isChecked() and self.pick_event(point.x() / 32, point.y() / 32):
                    return True
                self.wf_gesture = {'kind': 'marquee', 'start': point, 'end': point, 'additive': additive}
        else:
            self.wf_gesture = {'kind': tool, 'start': point, 'end': point, 'tiles': set(), 'last': tile}
            self.wf_add_stroke(tile)
        self.redraw(); self.wf_sync(); return True

    def wf_add_stroke(self, tile):
        g = self.wf_gesture
        if not g or g['kind'] in ('drag', 'marquee', 'duplicate'):
            return
        last = g['last']; steps = max(abs(tile[0] - last[0]), abs(tile[1] - last[1]), 1)
        size = self.wf_size.value(); ox, oz = self.view_data['context']['origin']
        if steps > 64:
            return
        for t in range(steps + 1):
            x = round(last[0] + (tile[0] - last[0]) * t / steps); z = round(last[1] + (tile[1] - last[1]) * t / steps)
            for xx in range(x, x + size):
                for zz in range(z, z + size):
                    if ox <= xx < ox + 32 and oz <= zz < oz + 32:
                        g['tiles'].add((xx, zz))
        g['last'] = tile

    def wf_move(self, point):
        if not self.wf_active():
            return False
        self.wf_hover = point
        if self.wf_gesture:
            self.wf_gesture['end'] = point
            ox, oz = self.view_data['context']['origin']
            self.wf_add_stroke((math.floor(point.x() / 32) + ox, math.floor(point.y() / 32) + oz))
        self.redraw(); return True

    def wf_release(self, point):
        if not self.wf_active():
            return False
        g = self.wf_gesture; self.wf_gesture = None
        if not g:
            return True
        g['end'] = point; kind = g['kind']; ox, oz = self.view_data['context']['origin']
        def finish():
            if kind == 'drag':
                dx = round((point.x() - g['start'].x()) / 32); dz = round((point.y() - g['start'].y()) / 32)
                if dx or dz:
                    self.wf_layout(dx, dz)
            elif kind == 'marquee':
                rect = QRectF(g['start'], point).normalized()
                if not g['additive']:
                    self.wf_ids.clear()
                if not self.wf_object_lock.isChecked() and self.toggles['Placements'].isChecked():
                    slots = {p['slot']: p['object_id'] for p in self.view_data['placements']}
                    for p in (self.scene_data or {}).get('placements', []):
                        f = p.get('footprint')
                        if f and rect.intersects(QRectF((f[0] - ox) * 32, (f[1] - oz) * 32, (f[2] - f[0]) * 32, (f[3] - f[1]) * 32)):
                            self.wf_ids.add(slots[p['slot']])
            else:
                tiles = g['tiles']
                if kind == 'rectangle':
                    x0, x1 = sorted((math.floor(g['start'].x() / 32), math.floor(point.x() / 32)))
                    z0, z1 = sorted((math.floor(g['start'].y() / 32), math.floor(point.y() / 32)))
                    tiles = {(x + ox, z + oz) for x in range(max(0, x0), min(31, x1) + 1) for z in range(max(0, z0), min(31, z1) + 1)}
                if kind == 'cells':
                    self.selected_cells ^= tiles
                elif tiles:
                    cells = [{'x': x, 'z': z} for x, z in sorted(tiles)]
                    if kind in ('block', 'unblock'):
                        self.wf_stage(self.wf_action('collision', {'cells': cells, 'blocked': kind == 'block'}))
                    else:
                        request = {'cells': cells, 'material': self.wf_material.currentData()}
                        if self.wf_sample and self.wf_sample['material'] == request['material']:
                            request['sample'] = {k:self.wf_sample[k] for k in ('x','z')}
                        self.wf_stage(self.wf_action('paint', request))
        self.wf_guard(finish); self.redraw(); self.wf_sync(); return True

    def wf_draw(self):
        if not self.wf_active() or not self.view_data:
            return
        ox, oz = self.view_data['context']['origin']; pen = QPen(QColor('#7de0ca'), 2); pen.setCosmetic(True)
        ids = {p['slot']: p['object_id'] for p in self.view_data['placements']}
        g = self.wf_gesture
        dx = dz = 0
        if g and g['kind'] in ('drag', 'duplicate'):
            dx = round((g['end'].x() - g['start'].x()) / 32); dz = round((g['end'].y() - g['start'].y()) / 32)
        for p in (self.scene_data or {}).get('placements', []):
            f = p.get('footprint')
            if ids.get(p['slot']) in self.wf_ids and f:
                self.graphics.addRect((f[0] - ox) * 32, (f[1] - oz) * 32, (f[2] - f[0]) * 32, (f[3] - f[1]) * 32, pen, QBrush(QColor(125, 224, 202, 25))).setZValue(12)
                if dx or dz:
                    self.graphics.addRect((f[0] - ox + dx) * 32, (f[1] - oz + dz) * 32, (f[2] - f[0]) * 32, (f[3] - f[1]) * 32, pen, QBrush(QColor(125, 224, 202, 70))).setZValue(13)
        if g and g['kind'] in ('marquee', 'rectangle'):
            self.graphics.addRect(QRectF(g['start'], g['end']).normalized(), pen, QBrush(QColor(125, 224, 202, 35))).setZValue(14)
        elif g and 'tiles' in g:
            for x, z in g['tiles']:
                self.graphics.addRect((x - ox) * 32, (z - oz) * 32, 32, 32, pen, QBrush(QColor(125, 224, 202, 45))).setZValue(14)
        elif self.wf_hover is not None and self.wf_tool.currentData() != 'select':
            x, z = math.floor(self.wf_hover.x() / 32), math.floor(self.wf_hover.y() / 32)
            if 0 <= x < 32 and 0 <= z < 32:
                size = self.wf_size.value()
                self.graphics.addRect(x * 32, z * 32, min(size, 32 - x) * 32, min(size, 32 - z) * 32, pen, QBrush(QColor(125, 224, 202, 25))).setZValue(14)
