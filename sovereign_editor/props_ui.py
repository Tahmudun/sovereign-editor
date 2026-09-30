"""Custom static props: import/reimport, place, duplicate, move, remove and collision.

Every action stages ONE Project operation (the same calls as the CLI: stage_prop_source,
plan_prop_edit, apply_prop_edit) and shows its preview plus a game-camera render of the
trial state before anything is written. Apply saves one undoable transaction; stale
revisions, stale asset revisions and collision conflicts are refused by Project.
"""
import copy
import math

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QPushButton, QListWidget,
                               QListWidgetItem, QLineEdit, QDoubleSpinBox, QPlainTextEdit, QFileDialog, QSplitter, QWidget, QSizePolicy)

from .formats import EditorError


def _pixmap(image, scale=2):
    image = image.convert('RGB').resize((image.width * scale, image.height * scale))
    data = image.tobytes()
    return QPixmap.fromImage(QImage(data, image.width, image.height, image.width * 3, QImage.Format.Format_RGB888).copy())


def parse_collision(text):
    cells = []
    for part in [p for p in text.replace(';', ' ').split() if p.strip()]:
        dx, dz = (int(v) for v in part.split(','))
        cells.append([dx, dz])
    return cells


class PropEditor(QDialog):
    def __init__(self, inspector):
        super().__init__(inspector)
        self.inspector = inspector; self.project = inspector.project
        self.header, self.cell = inspector.header, list(inspector.cell)
        self.pending = None; self.staged = None
        self.setWindowTitle('Sovereign Editor · Custom props')
        self.resize(1180, 820)
        layout = QVBoxLayout(self)
        self.title = QLabel(); self.title.setStyleSheet('font-size:20px;font-weight:600'); layout.addWidget(self.title)
        self.status = QLabel(); self.status.setWordWrap(True); layout.addWidget(self.status)
        split = QSplitter(); layout.addWidget(split, 1)
        left = QWidget(); ll = QVBoxLayout(left); split.addWidget(left)
        ll.addWidget(QLabel('Registered assets (project-owned packages)'))
        self.assets = QListWidget(); ll.addWidget(self.assets, 1)
        self.import_button = QPushButton('Import or reimport folder…'); ll.addWidget(self.import_button)
        self.asset_note = QLabel(); self.asset_note.setWordWrap(True); ll.addWidget(self.asset_note)
        ll.addWidget(QLabel('Instances in this map cell'))
        self.instances = QListWidget(); ll.addWidget(self.instances, 1)
        middle = QWidget(); ml = QVBoxLayout(middle); split.addWidget(middle)
        form = QFormLayout(); ml.addLayout(form)
        self.instance_name = QLineEdit(); form.addRow('Instance name', self.instance_name)
        self.x = QDoubleSpinBox(); self.z = QDoubleSpinBox()
        for box in (self.x, self.z):
            box.setDecimals(1); box.setSingleStep(1.0); box.setRange(0, 9999)
        row = QHBoxLayout(); row.addWidget(QLabel('X')); row.addWidget(self.x); row.addWidget(QLabel('Z')); row.addWidget(self.z)
        form.addRow('Anchor (tile + .5 = centre)', row)
        self.collision = QLineEdit(); self.collision.setPlaceholderText('dx,dz tile offsets, e.g. -1,0 0,0 1,0 (empty = none)')
        form.addRow('Explicit collision', self.collision)
        buttons = QHBoxLayout(); ml.addLayout(buttons)
        for attr, label, callback in (('place_button', 'Place new', self.stage_place), ('duplicate_button', 'Duplicate selected', self.stage_duplicate),
                                      ('move_button', 'Move selected', self.stage_move), ('collision_button', 'Set collision', self.stage_collision),
                                      ('remove_button', 'Remove selected', self.stage_remove)):
            b = QPushButton(label); setattr(self, attr, b); b.clicked.connect(lambda _=False, cb=callback: self.guard(cb)); buttons.addWidget(b)
        self.camera = QLabel(); self.camera.setAlignment(Qt.AlignmentFlag.AlignCenter); self.camera.setMinimumSize(384, 288)
        # The frame scales to the space available (720 px screens) without forcing the dialog size.
        self.camera.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored); self._frame = None
        ml.addWidget(self.camera, 1)
        self.preview = QPlainTextEdit(); self.preview.setReadOnly(True); self.preview.setMaximumHeight(170); layout.addWidget(self.preview)
        actions = QHBoxLayout(); layout.addLayout(actions)
        for attr, label, callback in (('apply_button', 'Apply', self.apply), ('discard_button', 'Discard preview', self.discard),
                                      ('undo_button', 'Undo', self.undo), ('redo_button', 'Redo', self.redo)):
            b = QPushButton(label); setattr(self, attr, b); b.clicked.connect(lambda _=False, cb=callback: self.guard(cb)); actions.addWidget(b)
        actions.addStretch(); close = QPushButton('Close'); close.clicked.connect(self.close); actions.addWidget(close)
        self.import_button.clicked.connect(lambda: self.guard(self.choose_import))
        self.instances.currentRowChanged.connect(lambda *_: self.guard(self.select_instance))
        self.assets.currentRowChanged.connect(lambda *_: self.guard(self.select_asset))
        self.reload()

    # ---- state ------------------------------------------------------------------------

    def guard(self, callback):
        try:
            return callback()
        except (EditorError, ValueError, OSError, KeyError, TypeError) as exc:
            self.status.setText(f'Refused: {exc}')
            self.pending = None; self.apply_button.setEnabled(False)

    def reload(self):
        self.view = self.project.prop_view()
        self.revision = self.project.doc['revision']
        self.title.setText(f"Custom props · header {self.header} cell {self.cell[0]},{self.cell[1]}")
        self.assets.blockSignals(True); self.assets.clear()
        for aid, a in sorted(self.view['assets'].items()):
            item = QListWidgetItem(f"{a['display']} · {aid} · r{a['revision']} · model {a['model_id']} · "
                                   f"{a['report']['triangles']} tris · {a['report']['texture_bytes']} B textures")
            item.setData(Qt.ItemDataRole.UserRole, aid); self.assets.addItem(item)
        self.assets.blockSignals(False)
        self.instances.blockSignals(True); self.instances.clear()
        for key, inst in sorted(self.view['instances'].items()):
            if inst['context']['header'] == self.header and inst['context']['cell'] == self.cell:
                item = QListWidgetItem(f"{key} · {inst['asset']} r{inst['revision']} · ({inst['x']:g}, {inst['z']:g}) · "
                                       f"collision {len(inst['cells'])} tiles")
                item.setData(Qt.ItemDataRole.UserRole, key); self.instances.addItem(item)
        self.instances.blockSignals(False)
        if self.assets.count() and self.assets.currentRow() < 0:
            self.assets.setCurrentRow(0)
        self.pending = None; self.apply_button.setEnabled(False)
        self.undo_button.setEnabled(bool(self.project.doc['history'])); self.redo_button.setEnabled(bool(self.project.doc.get('redo')))
        self.status.setText(f"Revision {self.revision} · Stage one action, preview it (text and game camera), then Apply. "
                            f"Limits: model IDs {self.view['limits']['model_ids']}, {self.view['limits']['objects_per_map']} objects per map.")
        self.render_camera(self.project, None)

    def selected_asset(self):
        item = self.assets.currentItem()
        if item is None:
            raise ValueError('Register or select an asset first.')
        return item.data(Qt.ItemDataRole.UserRole)

    def selected_instance(self):
        item = self.instances.currentItem()
        if item is None:
            raise ValueError('Select an instance in this cell first.')
        return item.data(Qt.ItemDataRole.UserRole)

    def select_asset(self):
        if self.assets.currentItem() is None:
            return
        a = self.view['assets'][self.selected_asset()]
        self.asset_note.setText(f"Package {a['package'][:16]}… · default collision {a['collision']} · textures "
                                f"{[t['name'] for t in a['report']['textures']]} · stock textures {a['report']['stock_textures']}")
        if not self.collision.text():
            self.collision.setText(' '.join(f'{dx},{dz}' for dx, dz in a['collision']))

    def select_instance(self):
        if self.instances.currentItem() is None:
            return
        inst = self.view['instances'][self.selected_instance()]
        self.x.setValue(inst['x']); self.z.setValue(inst['z'])
        self.collision.setText(' '.join(f'{dx},{dz}' for dx, dz in inst['collision']))
        self.render_camera(self.project, inst)

    # ---- staging ------------------------------------------------------------------------

    def stage(self, operations, focus=None):
        plan = self.project.plan_prop_edit(operations, context={'header': self.header, 'cell': self.cell})
        self.pending = operations
        lines = []
        for t in plan['transactions']:
            e = t.get('effect') or {}
            lines.append(f"{t['action']} · {t['request']}")
            if 'permissions' in e:
                lines.append(f"  collision changes: {[(c['x'], c['z'], c['before'], c['after']) for c in e['permissions']]}")
            if 'instances' in e:
                lines.append(f"  affected instances: {e['instances']} · {e['note']}")
        self.preview.setPlainText('\n'.join(lines) or 'No change.')
        self.apply_button.setEnabled(not plan['empty'])
        self.status.setText(f'Preview ready at revision {self.revision} · Apply saves one undoable edit.')
        trial = self.project.area_preview_project(plan)
        self.render_camera(trial, focus)

    def render_camera(self, project, inst):
        try:
            from . import prop_camera
            _, _, prims = prop_camera.scene_prims(project, self.header, self.cell)
            ctx = project.context(header=self.header, cell=self.cell)
            if inst is not None:
                focus = (math.floor(inst['x']) - ctx['origin'][0], math.floor(inst['z']) - ctx['origin'][1]); height = inst.get('height', 1.0)
            else:
                focus = (16, 16); height = 1.0
            self._frame = _pixmap(prop_camera.frame(prims, focus, height)); self._show_frame()
        except EditorError as exc:
            self.camera.setText(f'Game-camera preview unavailable: {exc}')

    def _show_frame(self):
        if self._frame is not None:
            self.camera.setPixmap(self._frame.scaled(self.camera.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                                     Qt.TransformationMode.FastTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event); self._show_frame()

    def choose_import(self):
        folder = QFileDialog.getExistingDirectory(self, 'Asset source folder (asset.json, OBJ, indexed PNGs)')
        if folder:
            self.stage_import(folder)

    def stage_import(self, folder):
        staged = self.project.stage_prop_source(folder)
        if staged['unchanged']:
            self.pending = None; self.apply_button.setEnabled(False)
            self.preview.setPlainText(f"Unchanged reimport of {staged['asset']}: the baked package is identical. Nothing to apply.")
            return staged
        self.staged = staged
        self.stage([staged['operation']])
        r = staged['report']
        self.preview.appendPlainText(f"\nBake: {r['triangles']} triangles, {r['materials']} materials, textures {r['textures']}, "
                                     f"{r['texture_bytes']} B, bounds {r['bounds_tiles']} tiles"
                                     + (f"\nRevision {staged['revision']} → {staged['revision'] + 1}: instances {staged['affected_instances']} "
                                        "change appearance; stock trees and other resources are untouched." if staged['registered'] else ''))
        return staged

    def stage_place(self):
        aid = self.selected_asset(); key = self.instance_name.text().strip()
        self.stage([{'action': 'place', 'asset': aid, 'instance': key, 'x': self.x.value(), 'z': self.z.value(),
                     'collision': parse_collision(self.collision.text()),
                     'expected_revision': self.view['assets'][aid]['revision']}],
                   {'x': self.x.value(), 'z': self.z.value(), 'height': 1.0})

    def stage_duplicate(self):
        self.stage([{'action': 'duplicate', 'source': self.selected_instance(), 'instance': self.instance_name.text().strip(),
                     'x': self.x.value(), 'z': self.z.value()}], {'x': self.x.value(), 'z': self.z.value(), 'height': 1.0})

    def stage_move(self):
        self.stage([{'action': 'move', 'instance': self.selected_instance(), 'x': self.x.value(), 'z': self.z.value()}],
                   {'x': self.x.value(), 'z': self.z.value(), 'height': 1.0})

    def stage_collision(self):
        key = self.selected_instance()
        self.stage([{'action': 'collision', 'instance': key, 'collision': parse_collision(self.collision.text())}], self.view['instances'][key])

    def stage_remove(self):
        key = self.selected_instance()
        self.stage([{'action': 'remove', 'instance': key}], self.view['instances'][key])

    # ---- commit ---------------------------------------------------------------------------

    def apply(self):
        if self.pending:
            self.project.apply_prop_edit(self.revision, self.pending, context={'header': self.header, 'cell': self.cell})
            self.reload()
            self.inspector.refresh()

    def discard(self):
        self.pending = None; self.preview.clear(); self.apply_button.setEnabled(False)
        self.status.setText(f'Revision {self.revision} · Preview discarded; nothing was written.')
        self.render_camera(self.project, None)

    def undo(self):
        self.project.undo(self.revision); self.reload(); self.inspector.refresh()

    def redo(self):
        self.project.redo(self.revision); self.reload(); self.inspector.refresh()
