"""Native Qt editor. Every persisted edit goes through core.Project."""
import json
import math
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QRectF, QPointF, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QBrush, QPen, QPainter, QPixmap, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QCheckBox, QListWidget, QListWidgetItem, QSpinBox, QDoubleSpinBox, QFormLayout,
    QGraphicsView, QGraphicsScene, QGraphicsEllipseItem, QGraphicsRectItem, QGraphicsItem,
    QSplitter, QFrame, QFileDialog, QPlainTextEdit, QScrollArea, QLayout, QSizePolicy,
)

from .core import Project, ORIGIN
from . import __version__
from .formats import EditorError

TILE = 24
COLORS = {"ink": "#e8edf1", "muted": "#93a4ad", "accent": "#85dbc0", "gold": "#e5bf7e", "blue": "#87bdf5"}


class ActorItem(QGraphicsEllipseItem):
    def __init__(self, npc, window):
        super().__init__(-10, -10, 20, 20)
        self.npc, self.window = npc, window
        self.setPos((npc["x"] - ORIGIN[0] + .5) * TILE, (npc["z"] - ORIGIN[1] + .5) * TILE)
        self.setZValue(10)
        self.setPen(QPen(QColor("#142a28"), 2))
        self.setBrush(QBrush(QColor(COLORS["accent"] if npc["editable"] else COLORS["blue"])))
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        if npc["editable"]:
            self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable)
        self.setToolTip(f"{npc['label']} · NPC {npc['id']}\nTile {npc['x']}, {npc['z']}" + ("\nDrag to move" if npc["editable"] else "\nInspect only"))

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        painter.setPen(QColor("#10211f"))
        font = painter.font()
        font.setBold(True)
        font.setPointSize(9)
        painter.setFont(font)
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, str(self.npc["id"]))
        if self.npc["id"] == self.window.selected_id:
            painter.setPen(QPen(QColor("#ffffff"), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(self.rect().adjusted(-4, -4, 4, 4))

    def mousePressEvent(self, event):
        self.window.select_npc(self.npc["id"])
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if self.npc["editable"]:
            x = int(self.pos().x() // TILE) + ORIGIN[0]
            z = int(self.pos().y() // TILE) + ORIGIN[1]
            # Rebuild after the current graphics event returns; never delete this item mid-event.
            QTimer.singleShot(0, lambda: self.window.move_selected(x, z))


class MapView(QGraphicsView):
    def __init__(self, parent):
        super().__init__(parent)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setBackgroundBrush(QColor("#171f24"))
        self.setFrameShape(QFrame.Shape.NoFrame)

    def wheelEvent(self, event):
        scale = self.transform().m11()
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        if .2 < scale * factor < 6:
            self.scale(factor, factor)
        event.accept()


class PlacementItem(QGraphicsRectItem):
    def __init__(self, placement, window):
        x0, z0, x1, z1 = placement["tile_bounds"]
        super().__init__(x0 * TILE - 2, z0 * TILE - 2, max((x1 - x0) * TILE + 4, 10), max((z1 - z0) * TILE + 4, 10))
        self.placement, self.window = placement, window
        self.setBrush(Qt.BrushStyle.NoBrush)
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        if placement["editable"]:
            self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable)
            self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setAcceptHoverEvents(True)
        # Door planes and small signs must remain selectable over a building.
        self.setZValue(2 + 1 / max((x1 - x0) * (z1 - z0), .2))
        self.setToolTip(f"{placement['label']} · {placement['key']}\n" +
                       ("Drag in whole tiles to a green anchor; release to save" if placement["editable"] else "Click to inspect stock model"))
        self.highlight(False)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange and self.placement["editable"]:
            return QPointF(math.floor(value.x() / TILE + .5) * TILE, math.floor(value.y() / TILE + .5) * TILE)
        return super().itemChange(change, value)

    def highlight(self, hover):
        selected = self.window.selected_placement == (self.placement["map"], self.placement["slot"])
        self.setPen(QPen(QColor("#ffe0a0"), 2) if selected or hover else QPen(Qt.PenStyle.NoPen))

    def hoverEnterEvent(self, event):
        self.highlight(True)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.highlight(False)
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        self.window.select_placement(self.placement["map"], self.placement["slot"])
        self.drag_revision = self.window.project.doc["revision"]
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if self.placement["editable"]:
            x, _, z = self.placement["global_position"]
            x, z = x + self.pos().x() / TILE, z + self.pos().y() / TILE
            key, revision = (self.placement["map"], self.placement["slot"]), self.drag_revision
            # Never destroy the graphics item while Qt is dispatching its event.
            QTimer.singleShot(0, lambda: self.window.move_placement_to(key, x, z, revision))


class ModelPreviewLabel(QLabel):
    def __init__(self):
        super().__init__()
        self.source = QPixmap()
        self.setFixedHeight(154)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def set_source(self, pixmap):
        self.source = pixmap
        self._fit()

    def _fit(self):
        self.setPixmap(self.source.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                         Qt.TransformationMode.SmoothTransformation) if not self.source.isNull() else QPixmap())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()


class EditorWindow(QMainWindow):
    def __init__(self, project=None):
        super().__init__()
        self.project = None
        self.selected_id = 1
        self.selected_placement = None
        self.placement_items = {}
        self.actors = {}
        self.scene_data = None
        self.setWindowTitle("Sovereign Editor")
        self.resize(1440, 900)
        self.setMinimumSize(1050, 650)
        self._layout()
        self._menus()
        if project:
            self.load_project(project)
        else:
            self.notice("Create a project from your HeartGold ROM, or open an existing workspace.")
            self._enabled(False)

    def _layout(self):
        outer = QWidget()
        layout = QVBoxLayout(outer)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        top = QWidget()
        top.setObjectName("topbar")
        header = QHBoxLayout(top)
        header.setContentsMargins(24, 16, 24, 16)
        brand = QLabel("SOVEREIGN  /  EDITOR")
        brand.setObjectName("brand")
        header.addWidget(brand)
        self.project_label = QLabel("World workspace")
        self.project_label.setObjectName("muted")
        header.addWidget(self.project_label)
        header.addStretch()
        open_button = QPushButton("Open project…")
        open_button.clicked.connect(self.open_dialog)
        header.addWidget(open_button)
        self.export_button = QPushButton("Export ROM…")
        self.export_button.setObjectName("primary")
        self.export_button.clicked.connect(self.export_dialog)
        header.addWidget(self.export_button)
        layout.addWidget(top)
        split = QSplitter()
        layout.addWidget(split, 1)
        left = QWidget()
        left.setMinimumWidth(190)
        left.setMaximumWidth(265)
        nav = QVBoxLayout(left)
        nav.setContentsMargins(18, 22, 14, 18)
        nav.addWidget(self.heading("WORLD"))
        nav.addWidget(QLabel("Cherrygrove City"))
        small = QLabel("Johto · two map cells\nHeader 67 / events 64")
        small.setObjectName("muted")
        nav.addWidget(small)
        nav.addSpacing(22)
        nav.addWidget(self.heading("CHARACTERS"))
        self.npc_list = QListWidget()
        self.npc_list.currentItemChanged.connect(self._list_selection)
        self.npc_list.setMaximumHeight(225)
        nav.addWidget(self.npc_list, 1)
        nav.addWidget(self.heading("STOCK MODELS · 15"))
        self.placement_list = QListWidget()
        self.placement_list.currentItemChanged.connect(self._placement_list_selection)
        nav.addWidget(self.placement_list, 1)
        legend = QLabel("●  Green: editable NPC\n●  Blue: inspect only\n□  Gold: selected model\n◇  Purple: door / warp")
        legend.setObjectName("muted")
        legend.setWordWrap(True)
        nav.addWidget(legend)
        nav.addSpacing(20)
        sprite = QPushButton("Aseprite workflow…")
        sprite.clicked.connect(self.sprite_dialog)
        nav.addWidget(sprite)
        split.addWidget(left)
        middle = QWidget()
        center = QVBoxLayout(middle)
        center.setContentsMargins(0, 0, 0, 0)
        center.setSpacing(0)
        tools = QHBoxLayout()
        tools.setContentsMargins(14, 10, 14, 10)
        self.toggles = {}
        for label, active in [("Grid", False), ("Collision", False), ("Buildings", True), ("Triggers", False), ("Events", True)]:
            check = QCheckBox(label)
            check.setChecked(active)
            check.toggled.connect(self.redraw)
            tools.addWidget(check)
            tools.addSpacing(7)
            self.toggles[label] = check
        tools.addStretch()
        fit = QPushButton("Fit")
        fit.clicked.connect(self.fit_map)
        tools.addWidget(fit)
        focus = QPushButton("East city")
        focus.clicked.connect(self.focus_east)
        tools.addWidget(focus)
        center.addLayout(tools)
        self.graphics = QGraphicsScene(self)
        self.view = MapView(self)
        self.view.setScene(self.graphics)
        center.addWidget(self.view, 1)
        caption = QLabel("  STOCK MAP & MODELS  ·  Click a model to inspect  ·  Scroll to zoom, drag background to pan")
        caption.setObjectName("caption")
        caption.setWordWrap(True)
        center.addWidget(caption)
        split.addWidget(middle)
        right = QWidget()
        inspector = QVBoxLayout(right)
        inspector.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        inspector.setContentsMargins(20, 22, 20, 16)
        inspector.addWidget(self.heading("INSPECTOR"))
        self.actor_name = QLabel("Select a character")
        self.actor_name.setWordWrap(True)
        self.actor_name.setObjectName("actorName")
        inspector.addWidget(self.actor_name)
        self.actor_info = QLabel("")
        self.actor_info.setWordWrap(True)
        self.actor_info.setObjectName("muted")
        inspector.addWidget(self.actor_info)
        inspector.addSpacing(16)
        self.position_fields = QWidget()
        form = QFormLayout(self.position_fields)
        form.setContentsMargins(0, 0, 0, 0)
        self.x_input = QSpinBox()
        self.z_input = QSpinBox()
        for widget in (self.x_input, self.z_input):
            widget.setRange(0, 65535)
        form.addRow("Tile X", self.x_input)
        form.addRow("Tile Z", self.z_input)
        inspector.addWidget(self.position_fields)
        self.apply_button = QPushButton("Apply position")
        self.apply_button.setObjectName("primary")
        self.apply_button.clicked.connect(lambda: self.move_selected(self.x_input.value(), self.z_input.value()))
        inspector.addWidget(self.apply_button)
        self.placement_fields = QWidget()
        placement_form = QFormLayout(self.placement_fields)
        placement_form.setContentsMargins(0, 0, 0, 0)
        self.placement_x_input, self.placement_z_input = QDoubleSpinBox(), QDoubleSpinBox()
        for widget, low, high in ((self.placement_x_input, 563.5, 565.5), (self.placement_z_input, 404.5, 405.5)):
            widget.setDecimals(1)
            widget.setSingleStep(1)
            widget.setRange(low, high)
        placement_form.addRow("Anchor X", self.placement_x_input)
        placement_form.addRow("Anchor Z", self.placement_z_input)
        self.placement_fields.hide()
        inspector.addWidget(self.placement_fields)
        self.placement_move_button = QPushButton("Apply planter position")
        self.placement_move_button.setObjectName("primary")
        self.placement_move_button.clicked.connect(lambda: self.move_selected_placement())
        self.placement_move_button.hide()
        inspector.addWidget(self.placement_move_button)
        self.placement_restore_button = QPushButton("Restore stock position")
        self.placement_restore_button.clicked.connect(lambda: self.move_selected_placement(565.5, 404.5))
        self.placement_restore_button.hide()
        inspector.addWidget(self.placement_restore_button)
        self.actor_hint = QLabel("Drag the green marker or enter coordinates. Edits are saved automatically.")
        self.actor_hint.setWordWrap(True)
        self.actor_hint.setObjectName("muted")
        inspector.addWidget(self.actor_hint)
        self.model_image = ModelPreviewLabel()
        self.model_image.hide()
        inspector.addWidget(self.model_image)
        self.model_details = QPlainTextEdit()
        self.model_details.setReadOnly(True)
        self.model_details.setMinimumHeight(100)
        self.model_details.setMaximumHeight(125)
        self.model_details.hide()
        inspector.addWidget(self.model_details)
        inspector.addSpacing(24)
        inspector.addWidget(self.heading("PROJECT CHANGES"))
        self.change_text = QPlainTextEdit()
        self.change_text.setReadOnly(True)
        self.change_text.setMaximumHeight(120)
        inspector.addWidget(self.change_text)
        self.undo_button = QPushButton("Undo last edit")
        self.undo_button.clicked.connect(self.undo)
        inspector.addWidget(self.undo_button)
        self.revision_label = QLabel("")
        self.revision_label.setObjectName("muted")
        inspector.addWidget(self.revision_label)
        inspector.addStretch()
        check = QPushButton("Validate project")
        check.clicked.connect(self.validate)
        inspector.addWidget(check)
        scope = QLabel(f"v{__version__} · NPC 1 + south planter movement\nOther models support inspection.")
        scope.setObjectName("muted")
        scope.setWordWrap(True)
        inspector.addWidget(scope)
        inspector_scroll = QScrollArea()
        inspector_scroll.setFrameShape(QFrame.Shape.NoFrame)
        inspector_scroll.setWidgetResizable(True)
        inspector_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inspector_scroll.setMinimumWidth(245)
        inspector_scroll.setMaximumWidth(320)
        inspector_scroll.setWidget(right)
        split.addWidget(inspector_scroll)
        split.setSizes([220, 950, 270])
        self.message = QLabel("")
        self.message.setObjectName("status")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.setCentralWidget(outer)

    def heading(self, text):
        label = QLabel(text)
        label.setObjectName("section")
        return label

    def _menus(self):
        file = self.menuBar().addMenu("File")
        for label, callback, shortcut in [("New project from ROM…", self.create_dialog, "Ctrl+N"),
                                          ("Open project…", self.open_dialog, "Ctrl+O"),
                                          ("Export ROM…", self.export_dialog, "Ctrl+E")]:
            action = QAction(label, self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(callback)
            file.addAction(action)
        maps = self.menuBar().addMenu("Maps")
        inspector = QAction("Map & permission inspector…", self)
        inspector.setShortcut(QKeySequence("Ctrl+M"))
        inspector.triggered.connect(self.open_map_inspector)
        maps.addAction(inspector)
        edit = self.menuBar().addMenu("Edit")
        undo = QAction("Undo", self)
        undo.setShortcut(QKeySequence.StandardKey.Undo)
        undo.triggered.connect(self.undo)
        edit.addAction(undo)
        reload = QAction("Reload project from disk", self)
        reload.triggered.connect(self.reload)
        edit.addAction(reload)
        assets = self.menuBar().addMenu("Assets")
        sprite = QAction("Open / export an Aseprite document…", self)
        sprite.triggered.connect(self.sprite_dialog)
        assets.addAction(sprite)

    def _enabled(self, enabled):
        for widget in (self.export_button, self.apply_button, self.undo_button, self.x_input, self.z_input):
            widget.setEnabled(enabled)

    def notice(self, text, error=False):
        self.message.setText(text)
        self.message.setStyleSheet("color: #f4ac9c;" if error else "color: #a6dbca;")

    def guard(self, operation):
        try:
            return operation()
        except (EditorError, OSError, ValueError, KeyError) as exc:
            self.notice(str(exc), True)
            return None

    def load_project(self, root):
        def load():
            project = Project(root)
            preview = project.preview()
            terrain = project.preview(include_models=False)
            self.project = project
            self.terrain = QPixmap(terrain["image"])
            self.stock_scene = QPixmap(preview["image"])
            self.project_label.setText(project.doc["name"])
            self.setWindowTitle(f"{project.doc['name']} — Sovereign Editor {__version__}")
            self._enabled(True)
            self.refresh()
            QTimer.singleShot(0, self.focus_east)
            self.notice("Project opened. Select the road trainer or south flower planter to edit; other models support inspection.")
        self.guard(load)

    def refresh(self):
        self.stock_scene = QPixmap(self.project.preview()["image"])
        self.scene_data = self.project.scene()
        self.placement_data = self.project.placements()["placements"]
        self.npc_list.blockSignals(True)
        self.npc_list.clear()
        for npc in self.scene_data["npcs"]:
            item = QListWidgetItem(f"{npc['id']:02d}   {npc['label']}" + ("  •" if npc["changed"] else ""))
            item.setData(Qt.ItemDataRole.UserRole, npc["id"])
            self.npc_list.addItem(item)
            if npc["id"] == self.selected_id:
                self.npc_list.setCurrentItem(item)
        self.npc_list.blockSignals(False)
        self.placement_list.blockSignals(True)
        self.placement_list.clear()
        for p in self.placement_data:
            item = QListWidgetItem(f"{p['key']}  {p['label']}" + ("  •" if p["changed"] else ""))
            item.setData(Qt.ItemDataRole.UserRole, (p["map"], p["slot"]))
            self.placement_list.addItem(item)
        self.placement_list.blockSignals(False)
        changes = self.project.diff()
        summaries = []
        for change in changes:
            if change["operation"] in ("npc.move", "placement.move"):
                title = (f"NPC {change['id']}" if change["operation"] == "npc.move"
                         else f"Planter {change['map']}:{change['slot']} + collision")
                summaries.append(title + f"\n{change['before']['x']}, {change['before']['z']} → "
                                 f"{change['after']['x']}, {change['after']['z']}")
            else:
                summaries.append(change["label"] + "\nOpen Maps → Map & permission inspector for details.")
        self.change_text.setPlainText("\n\n".join(summaries) or "No changes from the baseline.")
        self.revision_label.setText(f"Revision {self.project.doc['revision']} · saved to disk")
        self.undo_button.setEnabled(bool(self.project.doc["history"]))
        self.redraw()
        if self.selected_placement:
            self.select_placement(*self.selected_placement)
        else:
            self.select_npc(self.selected_id)

    def redraw(self, *_):
        if not self.scene_data:
            return
        self.graphics.clear()
        self.actors = {}
        self.placement_items = {}
        self.graphics.addPixmap(self.stock_scene if self.toggles["Buildings"].isChecked() else self.terrain)
        self.graphics.setSceneRect(0, 0, 64 * TILE, 32 * TILE)
        no_pen = QPen(Qt.PenStyle.NoPen)
        if self.toggles["Collision"].isChecked():
            for z, row in enumerate(self.scene_data["collision"]):
                for x, tile in enumerate(row):
                    if tile["blocked"]:
                        self.graphics.addRect(x * TILE, z * TILE, TILE, TILE, no_pen, QBrush(QColor(236, 96, 91, 90)))
        if self.toggles["Grid"].isChecked():
            pen = QPen(QColor(240, 255, 250, 65), .7)
            for x in range(65):
                self.graphics.addLine(x * TILE, 0, x * TILE, 32 * TILE, pen)
            for z in range(33):
                self.graphics.addLine(0, z * TILE, 64 * TILE, z * TILE, pen)
        self.graphics.addLine(32 * TILE, 0, 32 * TILE, 32 * TILE, QPen(QColor(235, 237, 201, 140), 2, Qt.PenStyle.DashLine))
        if self.toggles["Buildings"].isChecked():
            for p in self.placement_data:
                item = PlacementItem(p, self)
                self.graphics.addItem(item)
                self.placement_items[(p["map"], p["slot"])] = item
        self.anchor_marks = []
        if self.project.decoration_proof:
            for target in self.project.decoration_proof["allowed_anchors"]:
                x, z = (target["x"] - ORIGIN[0]) * TILE, (target["z"] - ORIGIN[1]) * TILE
                mark = self.graphics.addEllipse(x - 3, z - 3, 6, 6, QPen(QColor(COLORS["accent"]), 1.5),
                                                QBrush(QColor("#193d33")))
                mark.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
                mark.setZValue(8)
                mark.setVisible(self.selected_placement == (5, 14))
                self.anchor_marks.append(mark)
        if self.toggles["Triggers"].isChecked():
            for t in self.scene_data["triggers"]:
                mark = self.graphics.addRect((t["x"] - ORIGIN[0]) * TILE, (t["z"] - ORIGIN[1]) * TILE,
                                            t["width"] * TILE, t["height"] * TILE,
                                            QPen(QColor("#f4a76b"), 2), QBrush(QColor(245, 160, 95, 60)))
                mark.setToolTip(f"Scene trigger {t['id']} · script {t['script']}")
        if self.toggles["Events"].isChecked():
            for w in self.scene_data["warps"]:
                x, z = (w["x"] - ORIGIN[0] + .5) * TILE, (w["z"] - ORIGIN[1] + .5) * TILE
                mark = self.graphics.addRect(x - 6, z - 6, 12, 12, QPen(QColor("#e1a1f4"), 2), QBrush(QColor("#604478")))
                mark.setZValue(9)
                mark.setToolTip(f"Door {w['id']} → map header {w['destination']}, warp {w['destination_warp']}")
            for npc in self.scene_data["npcs"]:
                item = ActorItem(npc, self)
                self.graphics.addItem(item)
                self.actors[npc["id"]] = item

    def _list_selection(self, item, _):
        if item:
            self.select_npc(item.data(Qt.ItemDataRole.UserRole))

    def select_npc(self, npc_id):
        if not self.scene_data:
            return
        self.selected_id = npc_id
        self.selected_placement = None
        self.model_image.hide()
        self.model_details.hide()
        self.placement_move_button.hide()
        self.placement_fields.hide()
        self.placement_restore_button.hide()
        for mark in self.anchor_marks:
            mark.hide()
        self.position_fields.show()
        self.apply_button.show()
        npc = next(n for n in self.scene_data["npcs"] if n["id"] == npc_id)
        self.actor_name.setText(npc["label"])
        self.actor_info.setText(f"NPC {npc_id} · sprite {npc['sprite']}\nScript {npc['script']} · movement {npc['movement']}")
        self.x_input.setValue(npc["x"])
        self.z_input.setValue(npc["z"])
        for widget in (self.x_input, self.z_input, self.apply_button):
            widget.setEnabled(npc["editable"])
        self.actor_hint.setText("Drag the green marker or enter coordinates. Edits are saved automatically." if npc["editable"] else "Inspect only. This actor's scene or movement dependencies have not been qualified for editing.")
        self.npc_list.blockSignals(True)
        for row in range(self.npc_list.count()):
            item = self.npc_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == npc_id:
                self.npc_list.setCurrentItem(item)
        self.npc_list.blockSignals(False)
        self.placement_list.blockSignals(True)
        self.placement_list.setCurrentRow(-1)
        self.placement_list.blockSignals(False)
        for item in self.placement_items.values():
            item.highlight(False)
        self.graphics.update()

    def _placement_list_selection(self, item, _):
        if item:
            self.select_placement(*item.data(Qt.ItemDataRole.UserRole))

    def select_placement(self, map_id, slot):
        p = next(p for p in self.placement_data if p["map"] == map_id and p["slot"] == slot)
        self.selected_id, self.selected_placement = None, (map_id, slot)
        self.actor_name.setText(p["label"])
        x, y, z = p["global_position"]
        self.actor_info.setText(f"Placement {p['key']} · {p['model_name']}\nTile X {x:g} · Z {z:g} · height {y:g}\nRotation 0° · Scale 1 × 1 × 1")
        self.position_fields.hide()
        self.apply_button.hide()
        self.apply_button.setEnabled(False)
        self.placement_move_button.setVisible(p["editable"])
        self.placement_move_button.setEnabled(p["editable"])
        self.placement_fields.setVisible(p["editable"])
        self.placement_restore_button.setVisible(p["editable"])
        self.placement_restore_button.setEnabled(p["editable"] and p["changed"])
        if p["editable"]:
            self.placement_x_input.setValue(x)
            self.placement_z_input.setValue(z)
        for mark in self.anchor_marks:
            mark.setVisible(p["editable"])
        self.actor_hint.setText(
            "Drag to a green anchor or enter coordinates. X 563.5 / 564.5 at Z 404.5 / 405.5, plus stock. Three collision tiles move together. Saved automatically."
            if p["editable"] else "Read-only stock model. Collision and door dependencies must be qualified before moving it.")
        preview = self.guard(lambda: self.project.placement_preview(map_id, slot))
        self.model_image.set_source(QPixmap(str(preview)) if preview else QPixmap())
        self.model_image.show()
        dep, geom = p["dependencies"], p["geometry"]
        self.model_details.setPlainText(f"Model a/0/4/0 · member {p['model_id']}\nPlacement a/0/6/5 · member {map_id}\nRecord offset {p['record_offset']} · 48 bytes\n"
            f"{geom['vertices']} vertices · {geom['triangles']} triangles\n{len(p['textures'])} material bindings\n"
            f"Nearby doors: {dep['nearby_doors']}\nNearby triggers: {dep['nearby_triggers']}\n"
            f"Blocked tiles in bounds: {dep['blocked_tiles_in_bounds']}\nProximity does not establish ownership.")
        if p["editable"]:
            self.model_details.setPlainText(
                "Planter 5:14 · native model 52\nStock: 565.5, 404.5 · height 1\nFive qualified anchors; all others refuse.\n"
                "Three collision tiles follow the anchor; overlapping cells remain blocked.\n"
                "Native permissions, plate 6, full event/interaction checks and local walking connections.\n"
                "Game acceptance: pending per export. Guide scene remains untested.")
        self.model_details.show()
        for widget in (self.npc_list, self.placement_list):
            widget.blockSignals(True)
        self.npc_list.setCurrentRow(-1)
        for row in range(self.placement_list.count()):
            item = self.placement_list.item(row)
            if tuple(item.data(Qt.ItemDataRole.UserRole)) == self.selected_placement:
                self.placement_list.setCurrentItem(item)
        for widget in (self.npc_list, self.placement_list):
            widget.blockSignals(False)
        for item in self.placement_items.values():
            item.highlight(False)
        self.graphics.update()

    def move_selected(self, x, z):
        if not self.project or self.selected_id is None:
            return
        result = self.guard(lambda: self.project.move_npc(self.selected_id, x, z, self.project.doc["revision"]))
        if result is not None:
            self.notice(f"Saved NPC {self.selected_id} at tile {x}, {z}. Export a ROM to play this change.")
        # A refused drag must restore the visual marker to authoritative state.
        self.guard(self.refresh)

    def move_selected_placement(self, x=None, z=None):
        if not self.project or self.selected_placement is None:
            return
        p = next(p for p in self.placement_data if (p["map"], p["slot"]) == self.selected_placement)
        if not p["editable"]:
            return
        self.move_placement_to(self.selected_placement,
            self.placement_x_input.value() if x is None else x,
            self.placement_z_input.value() if z is None else z, self.project.doc["revision"])

    def move_placement_to(self, key, x, z, revision):
        result = self.guard(lambda: self.project.move_placement(*key, x, z, revision))
        # On refusal (including stale revision), rebuild from authoritative state.
        self.guard(self.refresh)
        if result is not None:
            self.notice("Saved planter position and collision together. Export the ROM for native game checks.")

    def undo(self):
        if not self.project:
            return
        result = self.guard(lambda: self.project.undo(self.project.doc["revision"]))
        if result is not None:
            self.refresh()
            self.notice("Last edit undone and saved.")

    def reload(self):
        if self.project:
            self.load_project(self.project.root)

    def open_map_inspector(self):
        """Any resolvable map context, through the same core operations as the CLI."""
        if not self.project:
            self.notice("Open a project first.", True)
            return
        from .map_inspector import MapInspectorWindow
        self.map_inspector = self.guard(lambda: MapInspectorWindow(self.project.root))
        if self.map_inspector:
            self.map_inspector.show()
        return self.map_inspector

    def validate(self):
        if self.project:
            result = self.guard(self.project.validate)
            if result:
                self.refresh()
                self.notice("File checks passed: baseline, resources, model bindings and movement clearance. Gameplay acceptance belongs to each exported ROM.")

    def fit_map(self):
        self.view.fitInView(self.graphics.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def focus_east(self):
        self.view.fitInView(QRectF(32 * TILE - 24, -24, 34 * TILE, 34 * TILE), Qt.AspectRatioMode.KeepAspectRatio)

    def open_dialog(self):
        root = QFileDialog.getExistingDirectory(self, "Open Sovereign project folder")
        if root:
            self.load_project(root)

    def create_dialog(self):
        rom, _ = QFileDialog.getOpenFileName(self, "Choose a HeartGold ROM", "", "Nintendo DS ROM (*.nds)")
        if not rom:
            return
        target, _ = QFileDialog.getSaveFileName(self, "New project folder name", str(Path.home() / "Documents" / "Cherrygrove"))
        if target:
            project = self.guard(lambda: Project.create(rom, target))
            if project:
                self.load_project(project.root)

    def export_dialog(self):
        if not self.project:
            return
        default = self.project.root / "exports" / f"candidate-r{self.project.doc['revision']}"
        target, _ = QFileDialog.getSaveFileName(self, "New ROM export folder", str(default))
        if target:
            # A project-local paired save can be supplied by the prepared example or CLI.
            save = self.project.root / "playtest.sav"
            result = self.guard(lambda: self.project.export(target, self.project.doc["revision"], save if save.is_file() else None))
            if result:
                self.notice(f"Exported {result['changed_byte_count']} changed byte(s) → {target}/game.nds")

    def sprite_dialog(self):
        source, _ = QFileDialog.getOpenFileName(self, "Choose an Aseprite document", "", "Aseprite (*.aseprite *.ase)")
        if not source:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(source))
        target, _ = QFileDialog.getSaveFileName(self, "Optional: new folder for sprite sheet export (Cancel to edit only)", str(Path(source).with_suffix("")) + "-export")
        if target:
            from .aseprite import export_sheet
            result = self.guard(lambda: export_sheet(source, target))
            if result:
                self.notice(f"Exported {result['frames']} sprite frames. ROM sprite import is a later checkpoint.")


STYLE = """
QWidget { background: #202a31; color: #e8edf1; font-family: 'Helvetica Neue'; font-size: 13px; }
QWidget#topbar { background: #172128; border-bottom: 1px solid #37444d; }
QLabel#brand { color: #d9b979; font-size: 16px; font-weight: 700; }
QLabel#muted { color: #93a4ad; font-size: 12px; }
QLabel#section { color: #9babb5; font-size: 11px; font-weight: 700; padding-bottom: 10px; }
QLabel#actorName { font-size: 21px; font-weight: 600; padding: 4px 0 8px 0; }
QLabel#caption { background: #182229; color: #9aadb7; font-size: 11px; padding: 12px 6px; }
QLabel#status { background: #152128; padding: 11px 22px; border-top: 1px solid #37444d; }
QPushButton { background: #2c3942; border: 1px solid #475861; border-radius: 5px; padding: 8px 12px; }
QPushButton:hover { background: #3b4b54; border-color: #91c9b9; }
QPushButton#primary { color: #152924; background: #85dbc0; border-color: #85dbc0; font-weight: 600; }
QPushButton#primary:hover { background: #a2ead4; }
QPushButton:disabled { color: #687984; background: #263139; border-color: #35424a; }
QPushButton#primary:disabled { color: #687984; background: #263139; border-color: #35424a; }
QPushButton#tool { padding: 6px 12px; }
QPushButton#tool:checked { color: #152924; background: #85dbc0; border-color: #85dbc0; font-weight: 600; }
QListWidget { background: #202a31; border: none; outline: 0; }
QListWidget::item { padding: 12px 4px; border-radius: 4px; }
QListWidget::item:selected { background: #354b50; color: #a7efd7; }
QSpinBox, QDoubleSpinBox, QPlainTextEdit { background: #182229; border: 1px solid #42545f; border-radius: 4px; padding: 7px; selection-background-color: #487a70; }
QSpinBox, QDoubleSpinBox { min-height: 24px; }
QSpinBox:disabled, QDoubleSpinBox:disabled { color: #7b8991; }
QPlainTextEdit { font-family: 'Menlo'; font-size: 11px; }
QSplitter::handle { background: #37444d; width: 1px; }
QScrollBar:vertical { background: #182229; width: 8px; margin: 0; }
QScrollBar:horizontal { background: #182229; height: 8px; margin: 0; }
QScrollBar::handle { background: #51656e; border-radius: 3px; min-height: 20px; min-width: 20px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }
QCheckBox { spacing: 6px; }
QCheckBox::indicator { width: 13px; height: 13px; border: 1px solid #788b96; border-radius: 3px; }
QCheckBox::indicator:checked { background: #85dbc0; border-color: #85dbc0; }
QMenu { background: #27353e; padding: 4px; }
QMenu::item:selected { background: #426358; }
"""


def launch(project=None, map_inspector=False):
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Sovereign Editor")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    if map_inspector:
        from .map_inspector import MapInspectorWindow
        inspector = MapInspectorWindow(project)
        inspector.show()
        QTimer.singleShot(0, lambda: print(
            f"Sovereign Editor map inspector ready: project={inspector.project.root}", flush=True))
        return app.exec()
    window = EditorWindow(project)
    window.show()
    QTimer.singleShot(0, lambda: print(
        f"Sovereign Editor ready: platform={app.platformName()}; project={window.project.root if window.project else 'none'}", flush=True))
    return app.exec()
