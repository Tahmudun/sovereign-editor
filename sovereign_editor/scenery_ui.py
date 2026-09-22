"""Scenery controls for the existing map inspector; all writes use Project."""
import copy
import json
import time

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QIcon, QBrush, QColor, QPen
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPushButton, QLineEdit, QComboBox, QAbstractItemView)

from . import scenery


class SceneryControls:
    def init_scenery(self):
        self.scenery_action = None
        self.scenery_scenes = {}
        self.scenery_plan_cells = []
        self.palette_dialog = None
        self.scenery_click_anchor = None

    def scenery_controls(self, side):
        row = QHBoxLayout()
        self.add_scenery_button = QPushButton('Add scenery…')
        self.add_scenery_button.clicked.connect(self.open_palette)
        row.addWidget(self.add_scenery_button)
        self.duplicate_button = QPushButton('Duplicate')
        self.duplicate_button.clicked.connect(lambda: self.start_scenery('duplicate'))
        row.addWidget(self.duplicate_button)
        self.delete_scenery_button = QPushButton('Remove')
        self.delete_scenery_button.clicked.connect(lambda: self.start_scenery('delete'))
        row.addWidget(self.delete_scenery_button)
        side.addLayout(row)
        row = QHBoxLayout()
        self.transfer_button = QPushButton('Transfer to neighbor')
        self.transfer_button.clicked.connect(lambda: self.start_scenery('transfer'))
        row.addWidget(self.transfer_button)
        self.destination_box = QComboBox()
        self.destination_box.setMinimumWidth(85)
        self.destination_box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.destination_box.currentIndexChanged.connect(self.destination_changed)
        row.addWidget(self.destination_box, 1)
        side.addLayout(row)
        hint = QLabel('Stage an action, choose a position, then Preview / Apply.\nCollision stays explicit; events are unchanged.')
        hint.setWordWrap(True)
        hint.setStyleSheet('font-size: 11px; color: #93a4ad;')
        side.addWidget(hint)

    def update_destinations(self):
        current = self.destination_box.currentData()
        self.destination_box.blockSignals(True)
        self.destination_box.clear()
        result = self.guard(lambda: self.project.map_neighborhood(header=self.header, cell=self.cell,
                                                                  image=False, include_grid=False, scenes=False))
        for entry in result['cells'] if result else []:
            if entry['active'] or entry['status'] != 'ok':
                continue
            target = {'header': entry['header'], 'cell': entry['cell']}
            self.destination_box.addItem(f"{entry['cell'][0]},{entry['cell'][1]} · h{entry['header']}", target)
            if target == current:
                self.destination_box.setCurrentIndex(self.destination_box.count()-1)
        self.destination_box.blockSignals(False)

    def open_palette(self):
        if self.scenery_action or self.staged_move() or self.staged_cells:
            self.notice('Apply or cancel the pending transaction before choosing another template.', True)
            return
        result = self.guard(lambda: self.project.map_palette(header=self.header, cell=self.cell, images=True))
        if result is None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('Scenery palette · ' + self.title_label.text())
        dialog.resize(660, 510)
        layout = QVBoxLayout(dialog)
        title = QLabel('Choose an existing scenery template')
        title.setStyleSheet('font-size: 18px; font-weight: 600;')
        layout.addWidget(title)
        hint = QLabel('Copies its height, transform and preserved fields. Text, doors and scripts are separate.')
        hint.setWordWrap(True);layout.addWidget(hint)
        search = QLineEdit();search.setPlaceholderText('Filter by name or model number…');layout.addWidget(search)
        listing = QListWidget()
        listing.setViewMode(QListWidget.ViewMode.IconMode)
        listing.setResizeMode(QListWidget.ResizeMode.Adjust)
        listing.setMovement(QListWidget.Movement.Static)
        listing.setIconSize(QSize(100, 74));listing.setGridSize(QSize(143, 136))
        listing.setWordWrap(True);listing.setTextElideMode(Qt.TextElideMode.ElideNone);listing.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        for template in result['templates']:
            item = QListWidgetItem(template.get('label') or template.get('name') or template['display'])
            item.setData(Qt.ItemDataRole.UserRole, template['slot'])
            item.setToolTip(f"Model {template['model_id']} · height {template['height']:g}\nDonor {result['context']['id']} slot {template['slot']}\n" +
                            (template.get('reason') or 'Click Use template, then click the map or enter an anchor.'))
            if template['status'] != 'ok':
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            elif template.get('thumbnail'):
                item.setIcon(QIcon(template['thumbnail']['image']))
            listing.addItem(item)
        layout.addWidget(listing, 1)
        search.textChanged.connect(lambda text: [listing.item(i).setHidden(text.lower() not in listing.item(i).text().lower())
                                                 for i in range(listing.count())])
        controls=QHBoxLayout();controls.addStretch()
        cancel=QPushButton('Close');cancel.clicked.connect(dialog.reject);controls.addWidget(cancel)
        use=QPushButton('Use template');use.setObjectName('primary');controls.addWidget(use);layout.addLayout(controls)
        def choose():
            item=listing.currentItem()
            if item and item.flags() & Qt.ItemFlag.ItemIsEnabled:
                slot=item.data(Qt.ItemDataRole.UserRole)
                dialog.accept();self.start_scenery('add', slot=slot)
        use.clicked.connect(choose);listing.itemDoubleClicked.connect(lambda _: choose())
        self.palette_dialog=dialog
        self.palette_list=listing
        dialog.setModal(True)
        dialog.show()

    def start_scenery(self, operation, slot=None):
        if self.scenery_action or self.staged_move():
            self.notice('Apply or cancel the pending object action first.', True)
            return
        slot = self.selected_slot if slot is None else slot
        if slot is None:
            self.notice('Select an object first.', True);return
        if operation == 'add':
            context=self.project.context(header=self.header,cell=self.cell)
            prop=self.project.member_data(context['map_member'])[1][slot]
            from .authoring import global_position
            position=global_position(context,prop)
        else:
            placement=next((p for p in self.view_data['placements'] if p['slot']==slot),None)
            if not placement or not placement['editable'] or self._sign_selected():
                self.notice('This object has a protected binding; use its existing controls.',True);return
            position=placement['position']
        destination=self.destination_box.currentData() if operation=='transfer' else None
        if operation=='transfer' and destination is None:
            self.notice('No supported neighboring cell is available.',True);return
        # Validate the source and destination before entering staging. Transfer's
        # initial point preserves the local anchor in the destination cell.
        x,z=position['x'],position['z']
        if destination:
            target=self.project.context(**destination)
            x += target['origin'][0]-self.view_data['context']['origin'][0]
            z += target['origin'][1]-self.view_data['context']['origin'][1]
        elif operation in ('add','duplicate'):
            ox=self.view_data['context']['origin'][0]
            x=min(x+1,ox+31.5)
        kwargs={'operation':operation,'slot':slot,'header':self.header,'cell':self.cell,
                'destination':destination,'x':None if operation=='delete' else x,'z':None if operation=='delete' else z}
        plan=self.guard(lambda:self.project.plan_scenery_edit(**kwargs))
        if plan is None:
            return
        self.scenery_click_anchor = None
        self.scenery_action={'operation':operation,'slot':slot,'destination':destination,'source':dict(position)}
        self.align_sign.setChecked(False)
        self.scenery_scenes={}
        if operation!='add':
            self.select_placement(slot)
        else:
            self.selected_slot = None
            self.object_label.setText(f"Add scenery · model {prop['model_id']}")
            self.object_detail.setText(f"Template slot {slot} · height {position['y']:g}. Click a location in the map.")
            from PySide6.QtGui import QPixmap
            thumbnail = self.guard(lambda:self.project.map_thumbnail(prop['model_id'], header=self.header, cell=self.cell))
            if thumbnail:
                self.thumbnail.setPixmap(QPixmap(thumbnail['image']).scaled(108,92,Qt.AspectRatioMode.KeepAspectRatio))
        self.x_input.set_anchor(x);self.z_input.set_anchor(z)
        if operation=='transfer':
            self.neighbors_toggle.setChecked(True)
            self.fit_neighbors()
        self.set_tool('select')
        self._describe_pending();self.redraw()
        self.notice('Action staged. Click the map or enter X/Z; Preview renders the proposed scene. Apply saves it.')

    def destination_changed(self, *_):
        if self.scenery_action and self.scenery_action['operation']=='transfer':
            target=self.destination_box.currentData()
            if target:
                self.scenery_action['destination']=target
                context=self.project.context(**target)
                source=self.scenery_action['source']
                self.x_input.set_anchor(source['x']+context['origin'][0]-self.view_data['context']['origin'][0])
                self.z_input.set_anchor(source['z']+context['origin'][1]-self.view_data['context']['origin'][1])
                self.scenery_scenes={};self.scenery_plan_cells=[];self.redraw();self._describe_pending()

    def place_scenery_at(self, local_x, local_z):
        if not self.scenery_action:
            return False
        if self.scenery_action['operation']=='delete':
            self.notice('Removal is staged. Use Preview / Apply or Cancel.');return True
        ox,oz=self.view_data['context']['origin']
        self.scenery_click_anchor = (time.monotonic(), (int(local_x//1), int(local_z//1)),
                                     self.x_input.anchor_value(), self.z_input.anchor_value())
        self.x_input.set_anchor(ox+int(local_x//1)+0.5)
        self.z_input.set_anchor(oz+int(local_z//1)+0.5)
        self.scenery_scenes={};self.scenery_plan_cells=[];self.redraw();self._describe_pending()
        return True

    def restore_scenery_double_click(self, local_x, local_z):
        """A double-click selects a blocker; undo its first click's placement."""
        click = self.scenery_click_anchor
        self.scenery_click_anchor = None
        if not self.scenery_action or click is None:
            return
        from PySide6.QtWidgets import QApplication
        when, cell, x, z = click
        if cell == (int(local_x), int(local_z)) and time.monotonic() - when <= QApplication.doubleClickInterval()/1000 + 0.15:
            self.x_input.set_anchor(x)
            self.z_input.set_anchor(z)
            self.scenery_scenes = {}
            self.scenery_plan_cells = []

    def scenery_request(self):
        action=self.scenery_action
        return {'operation':action['operation'],'slot':action['slot'],'header':self.header,'cell':self.cell,
                'x':None if action['operation']=='delete' else self.x_input.anchor_value(),
                'z':None if action['operation']=='delete' else self.z_input.anchor_value(),
                'destination':action['destination'], 'permissions':self._staged_permissions(),
                'move_collision':[{'x':x,'z':z} for x,z in sorted(self.selected_cells)]
                    if self.move_collision.isChecked() and action['operation']!='delete' else []}

    def show_scenery_plan(self, preview, saved=False):
        lines=[('SAVED' if saved else 'PENDING')+' · '+preview['label'],
               f"{preview['action']}: {preview['context']['id']} → {preview['destination_context']['id']}"]
        if preview['to']:
            lines.append(f"Target {preview['to']['x']:g}, {preview['to']['z']:g}; height {preview['to']['y']:g}")
        lines += [f"{o['action']} {o['id']} (authoring slot {o['slot']})" for o in preview['objects']]
        lines += [f"cell {c['x']},{c['z']}: {c['before']} → {c['after']}" for c in preview['permission_cells']]
        lines += ['', 'Events, height and unrelated resources remain unchanged.',
                  'Collision changes above are explicit. Shared map occurrences receive the same resource edit.']
        self.detail.setPlainText('\n'.join(lines))

    def preview_scenery(self):
        self.scenery_scenes = {}
        plan=self.guard(lambda:self.project.plan_scenery_edit(**self.scenery_request()))
        if plan is None:return
        shadow=copy.copy(self.project)
        shadow.doc=copy.deepcopy(self.project.doc)
        if not plan['empty']:shadow.doc['map_edits'].append(plan['transaction'])
        shadow._composed_cache=None
        contexts=[plan['transaction']['context'],plan['transaction']['destination_context']]
        for context in contexts:
            key=(context['header'],tuple(context['cell']))
            if key not in self.scenery_scenes:
                scene=self.guard(lambda:shadow.map_scene(header=key[0],cell=list(key[1])))
                if scene:self.scenery_scenes[key]=scene
        self.scenery_plan_cells = plan['transaction']['permissions']
        self.show_scenery_plan(plan['preview'])
        self._describe_pending()
        self.redraw();self.notice('Proposed scenery is rendered. Nothing saved; Apply or Cancel.')

    def apply_scenery(self):
        result=self.guard(lambda:self.project.apply_scenery_edit(self.project.doc['revision'],**self.scenery_request()))
        if result is None:return
        preview=result['preview']
        target=preview['destination_context']
        selected=next((o['slot'] for o in reversed(preview['objects']) if o['action']!='remove'),None)
        self._clear_staged()
        if target['header']!=self.header or target['cell']!=self.cell:
            self.load_context(target['header'],target['cell'],preserve_view=True)
        else:
            self.refresh()
        if selected is not None:self.select_placement(selected)
        self.show_scenery_plan(preview,saved=result['changed'])
        self.notice(f"Saved scenery at revision {result['revision']}. Export a ROM for native testing.")

    def draw_scenery_action(self, ox, oz):
        if not self.scenery_action:return
        for cell in self.scenery_plan_cells:
            blocked = bool(int(cell['after'][2:],16) & 128)
            color = QColor('#f4ac9c' if blocked else '#85dbc0')
            item = self.graphics.addRect((cell['x']-ox)*32+1,(cell['z']-oz)*32+1,30,30,
                QPen(color,2),QBrush(QColor(214,92,84,125) if blocked else QColor(133,219,192,100)))
            item.setZValue(11)
            item.setToolTip(f"{cell['x']},{cell['z']}: {cell['before']} → {cell['after']}")
        action=self.scenery_action
        source=action['source']
        points=[(source,'REMOVE' if action['operation'] in ('delete','transfer') else 'SOURCE')]
        if action['operation']!='delete':
            points.append(({'x':self.x_input.anchor_value(),'z':self.z_input.anchor_value()},'PLACE'))
        for point,text in points:
            color=QColor('#f4ac9c' if text=='REMOVE' else '#85dbc0')
            px,pz=(point['x']-ox)*32,(point['z']-oz)*32
            ring=self.graphics.addRect(px-16,pz-16,32,32,QPen(color,3,Qt.PenStyle.DashLine),QBrush(QColor(133,219,192,50)))
            ring.setZValue(12)
            label=self.graphics.addSimpleText(f"{text} {point['x']:g}, {point['z']:g}")
            label.setBrush(color);label.setPos(px,pz);label.setZValue(13)
            from PySide6.QtWidgets import QGraphicsItem
            from PySide6.QtGui import QFont
            label.setFont(QFont('Arial',10,QFont.Weight.Bold))
            label.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
            label.setTransform(label.transform().translate(12,12 if text=='PLACE' else -32))
            self._label_background(label)
