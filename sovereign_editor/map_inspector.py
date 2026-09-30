"""Native textured map inspector and editor.

Every write goes through the same ``core.Project`` operations the agent CLI uses:
``plan_map_edit`` / ``apply_map_edit`` / ``undo`` / ``export``.

The scene is the selected context's own terrain and building models, textured from
its area tilesets (see :mod:`sovereign_editor.mapscene`). Nothing is drawn from a
synthetic background, and a model this decoder cannot support is named in the
inspector while the rest of the scene and the whole permission grid stay usable.

Collision is drawn as the aggregate grid it is; nothing here infers which record
owns a blocked cell. The cells you paint are the cells you selected, and one Apply
sends the staged placement move and the staged cells as a single transaction.
"""
import json
from pathlib import Path

from PySide6.QtCore import Qt, QRectF, QTimer
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QKeySequence, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
    QGraphicsItem, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QPlainTextEdit, QPushButton, QScrollArea,
    QSizePolicy, QSpinBox, QSplitter, QVBoxLayout, QWidget, QTabWidget,
)

from .core import Project
from .formats import EditorError
from .scenery_ui import SceneryControls
from .event_ui import EventControls
from .workflow_ui import WorkflowControls
from . import scenery

TILE = 32
PAGE = 200
SNAP = 0.5
# Blocked cells get a light wash plus an outline, so roof and tree textures stay legible.
BLOCKED = QColor(214, 92, 84, 44)
BLOCKED_EDGE = QColor(236, 120, 108, 150)
OPEN = QColor(133, 219, 192, 26)
SELECTED = QColor(133, 219, 192, 110)
CHANGED = QColor(229, 191, 126, 150)
STAGED_BLOCK = QColor(214, 92, 84, 150)
STAGED_OPEN = QColor(133, 219, 192, 150)
PLACEMENT = QColor("#e5bf7e")
MOVED = QColor("#85dbc0")
GHOST = QColor(133, 219, 192, 60)
UNSUPPORTED = QColor("#f4ac9c")
LEGEND = [("Blocked", BLOCKED), ("Selected cell", SELECTED), ("Saved change", CHANGED),
          ("Staged block", STAGED_BLOCK), ("Staged unblock", STAGED_OPEN),
          ("Placement", PLACEMENT), ("Moved / staged", MOVED), ("Unsupported model", UNSUPPORTED)]
SELECT, BLOCK, UNBLOCK = "select", "block", "unblock"


class AnchorSpinBox(QDoubleSpinBox):
    """Anchor control that stores every 16.16 record coordinate exactly.

    ``k / 65536`` is ``k * 5**16 / 10**16``, so sixteen fractional decimals represent
    each record fraction exactly and the control never rounds one away. The value the
    author sees is the value that is sent: nothing here silently snaps a destination
    to a different record step. Core refuses a target that is not representable.
    """

    DECIMALS = 16

    def __init__(self):
        super().__init__()
        self.setDecimals(self.DECIMALS)
        self.setSingleStep(1)
        self.setRange(0, 65535)
        self._anchor = 0.0
        self._displayed = 0.0

    def set_anchor(self, value):
        self._anchor = value
        self.blockSignals(True)
        self.setValue(value)
        self.blockSignals(False)
        self._displayed = self.value()

    def anchor_value(self):
        # An untouched axis returns its source fraction bit for bit.
        return self._anchor if self.value() == self._displayed else self.value()

    def changed(self):
        return self.value() != self._displayed

    def textFromValue(self, value):
        return f"{value:.{self.DECIMALS}f}".rstrip("0").rstrip(".") or "0"

    def valueFromText(self, text):
        try:
            return float(text.strip())
        except ValueError:
            return self.value()


class SceneView(QGraphicsView):
    """Pan/zoom view of the textured scene, with tool-dependent mouse handling."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.dragging = None
        self.painting = False
        self.panning = None
        self.space_held = False
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setBackgroundBrush(QColor("#141b20"))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

    def tile_at(self, point):
        return int(point.x() // TILE), int(point.y() // TILE)

    def update_cursor(self):
        if self.panning is not None:
            shape = Qt.CursorShape.ClosedHandCursor
        elif self.space_held:
            shape = Qt.CursorShape.OpenHandCursor
        elif self.window.tool == SELECT:
            shape = Qt.CursorShape.ArrowCursor
        else:
            shape = Qt.CursorShape.CrossCursor
        self.viewport().setCursor(shape)

    def wheelEvent(self, event):
        self.window._fit_mode = None
        scale = self.transform().m11()
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        if .2 < scale * factor < 8:
            self.scale(factor, factor)
        event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if getattr(self.window, '_fit_mode', None) and not getattr(self.window, '_fit_pending', False):
            self.window._fit_pending = True
            QTimer.singleShot(0, self.window.refit_scene)

    def keyPressEvent(self, event):
        if self.window.wf_active():
            directions = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0),
                          Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1)}
            if event.key() in directions:
                self.window.wf_guard(lambda: self.window.wf_layout(*directions[event.key()]))
                event.accept(); return
            if event.key() == Qt.Key.Key_Escape:
                self.window.wf_gesture = None; self.window.redraw(); event.accept(); return
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self.space_held = True
            self.update_cursor()
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self.space_held = False
            self.update_cursor()
            event.accept()
            return
        super().keyReleaseEvent(event)

    def mousePressEvent(self, event):
        # Middle drag, or Space + left drag, always pans: never a selection or edit.
        if event.button() == Qt.MouseButton.MiddleButton or (
                event.button() == Qt.MouseButton.LeftButton and self.space_held):
            self._begin_pan(event)
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        point = self.mapToScene(event.position().toPoint())
        if self.window.wf_press(point, event.modifiers()):
            event.accept(); return
        if self.window.tool == SELECT:
            if self.window.place_scenery_at(point.x() / TILE, point.y() / TILE):
                event.accept()
                return
            if self.window.activate_neighbor(point.x() / TILE, point.y() / TILE):
                event.accept()
                return
            if self.window.inspector_tabs.currentIndex() == 1 and self.window.pick_event(point.x() / TILE, point.y() / TILE):
                event.accept()
                return
            slot = self.window.pick_placement(point.x() / TILE, point.y() / TILE)
            if slot is not None:
                self.window.select_placement(slot)
                self.dragging = (slot, point)
                event.accept()
                return
            if self.window.pick_event(point.x() / TILE, point.y() / TILE):
                event.accept()
                return
            # Open ground pans too, so a missed click never stages anything.
            self._begin_pan(event)
            return
        self.painting = True
        self.window.paint_tile(*self.tile_at(point))
        event.accept()

    def _begin_pan(self, event):
        self.window._fit_mode = None
        self.panning = event.position().toPoint()
        self.update_cursor()
        event.accept()

    def mouseMoveEvent(self, event):
        if self.panning is not None:
            position = event.position().toPoint()
            delta = position - self.panning
            self.panning = position
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            event.accept()
            return
        if self.window.wf_move(self.mapToScene(event.position().toPoint())):
            event.accept(); return
        if self.dragging is not None:
            slot, origin = self.dragging
            point = self.mapToScene(event.position().toPoint())
            self.window.drag_placement(slot, (point.x() - origin.x()) / TILE,
                                       (point.y() - origin.y()) / TILE)
            event.accept()
            return
        if self.painting:
            point = self.mapToScene(event.position().toPoint())
            self.window.paint_tile(*self.tile_at(point), repeated=True)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        was_panning = self.panning is not None
        self.dragging, self.painting, self.panning = None, False, None
        if not was_panning and event.button() == Qt.MouseButton.LeftButton and self.window.wf_release(self.mapToScene(event.position().toPoint())):
            event.accept(); return
        self.update_cursor()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self.window.wf_active() and not self.space_held and event.button() == Qt.MouseButton.LeftButton:
            event.accept(); return
        # Only a plain left double-click selects a cell. The second press of a middle
        # or Space+left pan keeps panning and never touches cells or anchors.
        if event.button() == Qt.MouseButton.MiddleButton or (
                event.button() == Qt.MouseButton.LeftButton and self.space_held):
            self._begin_pan(event)
            return
        if event.button() != Qt.MouseButton.LeftButton:
            event.accept()
            return
        point = self.mapToScene(event.position().toPoint())
        self.window.restore_scenery_double_click(*self.tile_at(point))
        self.window.toggle_cell(*self.tile_at(point))
        event.accept()


class MapInspectorWindow(WorkflowControls, EventControls, SceneryControls, QMainWindow):
    """Context browser, textured scene, placement/permission editing, one transaction."""

    def __init__(self, project, context=None):
        super().__init__()
        self.project = project if isinstance(project, Project) else Project(project)
        self.init_workflow()
        self.init_scenery()
        self.init_events()
        self.view_data = None
        self.scene_data = None
        self.scene_error = None
        self.neighborhood_data = None
        self.sign_data = {"supported": False}
        self.selected_cells = set()
        self.staged_cells = {}
        self.selected_slot = None
        self.tool = SELECT
        self.page = 0
        self._fit_mode = 'content'
        self.setWindowTitle("Sovereign Editor · textured map editor")
        self.resize(1440, 900)
        self._layout()
        self.reload_cells()
        # The pinned ROM's Cherrygrove east cell is only the opening view, not a limit.
        saved = self.project.doc.get("map_selection")
        default = (saved["header"], saved["cell"]) if saved else (67, [17, 12])
        header, cell = context or default
        self.load_context(header, list(cell))
        if context is None and saved and "event_kind" in saved:
            self.select_event(saved["event_kind"], saved["event_id"])
        if context is None and saved and saved.get('workspace'):
            self.inspector_tabs.setCurrentIndex(2)

    # ---- widgets -------------------------------------------------------------

    def _project_menu(self):
        # Menu bar entries cost no width, so the compact 1180 px layout is unchanged.
        menu = self.project_menu = self.menuBar().addMenu('Project')
        for label, slot, shortcut in (('Project browser…', self.open_browser, 'Ctrl+B'),
                                      ('Checkpoints and packages…', self.open_checkpoints, 'Ctrl+Shift+K')):
            action = QAction(label, self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(slot)
            menu.addAction(action)

    def _layout(self):
        self._project_menu()
        split = QSplitter()
        split.addWidget(self._browser())
        split.addWidget(self._scene_panel())
        self.inspector_tabs = QTabWidget()
        self.inspector_tabs.setMinimumWidth(300)
        self.inspector_tabs.setMaximumWidth(390)
        self.inspector_tabs.addTab(self._inspector(), "Scenery")
        self.inspector_tabs.addTab(self.event_panel(), "Events")
        self.inspector_tabs.addTab(self.workflow_panel(), "Layout")
        self.inspector_tabs.currentChanged.connect(self.event_tab_changed)
        self.inspector_tabs.currentChanged.connect(self.workflow_tab_changed)
        split.addWidget(self.inspector_tabs)
        split.setSizes([260, 860, 340])
        split.setStretchFactor(1, 1)
        split.setChildrenCollapsible(False)
        self.setCentralWidget(split)
        self.message = QLabel("")
        self.message.setObjectName("status")
        self.message.setWordWrap(True)
        self.statusBar().addWidget(self.message, 1)

    def _browser(self):
        left = QWidget()
        left.setMinimumWidth(210)
        left.setMaximumWidth(300)
        nav = QVBoxLayout(left)
        nav.addWidget(self.heading("MAP CONTEXT"))
        row = QHBoxLayout()
        row.addWidget(QLabel("Matrix"))
        self.matrix_input = QSpinBox()
        self.matrix_input.setRange(0, 4095)
        row.addWidget(self.matrix_input)
        listing = QPushButton("List cells")
        listing.clicked.connect(lambda: self.reload_cells(0))
        row.addWidget(listing)
        nav.addLayout(row)
        header_row = QHBoxLayout()
        header_row.addWidget(QLabel("Header"))
        self.header_input = QSpinBox()
        self.header_input.setRange(0, 65535)
        self.header_input.setValue(67)
        header_row.addWidget(self.header_input)
        open_header = QPushButton("Open")
        open_header.clicked.connect(self.open_header)
        header_row.addWidget(open_header)
        nav.addLayout(header_row)
        world_button = QPushButton('World: areas, connections, terrain…')
        world_button.setToolTip('Create areas from templates, connect entrances and edit terrain.')
        world_button.clicked.connect(self.open_world)
        nav.addWidget(world_button)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search name or map member…")
        self.search_input.returnPressed.connect(lambda: self.reload_cells(0))
        self.search_input.editingFinished.connect(lambda: self.reload_cells(0))
        nav.addWidget(self.search_input)
        self.cell_list = QListWidget()
        self.cell_list.currentItemChanged.connect(self._cell_selected)
        nav.addWidget(self.cell_list, 1)
        paging = QHBoxLayout()
        self.previous_button = QPushButton("‹ Prev")
        self.previous_button.clicked.connect(lambda: self.reload_cells(self.page - 1))
        paging.addWidget(self.previous_button)
        self.page_label = QLabel("")
        self.page_label.setObjectName("muted")
        paging.addWidget(self.page_label, 1)
        self.next_button = QPushButton("Next ›")
        self.next_button.clicked.connect(lambda: self.reload_cells(self.page + 1))
        paging.addWidget(self.next_button)
        nav.addLayout(paging)
        self.context_label = QLabel("")
        self.context_label.setWordWrap(True)
        self.context_label.setObjectName("muted")
        nav.addWidget(self.context_label)
        # Unsupported resources stay visible; the archive/member detail folds away.
        self.unsupported_label = QLabel("")
        self.unsupported_label.setWordWrap(True)
        self.unsupported_label.setStyleSheet("color: #f4ac9c; font-size: 11px;")
        nav.addWidget(self.unsupported_label)
        self.resource_toggle = QPushButton("Scene resources ▸")
        self.resource_toggle.setCheckable(True)
        self.resource_toggle.toggled.connect(self._toggle_resources)
        nav.addWidget(self.resource_toggle)
        self.resource_label = QLabel("")
        self.resource_label.setWordWrap(True)
        self.resource_label.setObjectName("muted")
        self.resource_label.setVisible(False)
        nav.addWidget(self.resource_label)
        return left

    def _toggle_resources(self, shown):
        self.resource_label.setVisible(shown)
        self.resource_toggle.setText("Scene resources ▾" if shown else "Scene resources ▸")

    def _scene_panel(self):
        middle = QWidget()
        center = QVBoxLayout(middle)
        center.setContentsMargins(0, 0, 0, 0)
        center.setSpacing(0)
        title_row = QHBoxLayout()
        title_row.setContentsMargins(12, 8, 12, 0)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.title_label = QLabel("")
        self.title_label.setStyleSheet("font-size: 19px; font-weight: 600;")
        titles.addWidget(self.title_label)
        self.subtitle_label = QLabel("")
        self.subtitle_label.setObjectName("muted")
        self.subtitle_label.setWordWrap(True)
        titles.addWidget(self.subtitle_label)
        title_row.addLayout(titles, 1)
        props_button = QPushButton('Custom props…')
        props_button.setToolTip('Import, place, duplicate, move, remove and revise custom static props.')
        props_button.clicked.connect(self.open_props)
        title_row.addWidget(props_button)
        action_row = QHBoxLayout()
        action_row.setContentsMargins(12, 4, 12, 0)
        area = QPushButton('Author area…')
        area.setToolTip('Compose surfaces, groups, stock templates and simple dialogue in one edit.')
        area.clicked.connect(self.open_area_authoring)
        action_row.addWidget(area)
        story = QPushButton('Characters & events…')
        story.clicked.connect(self.open_story_authoring)
        action_row.addWidget(story)

        gameplay_button = QPushButton('Teams & encounters…')
        gameplay_button.clicked.connect(self.open_gameplay)
        action_row.addWidget(gameplay_button)
        layout_button = QPushButton('Edit layout')
        layout_button.clicked.connect(lambda: self.inspector_tabs.setCurrentIndex(2))
        action_row.addWidget(layout_button)
        fit_content = QPushButton("Fit area")
        fit_content.setToolTip("Zoom to the drawn room or map content inside this cell.")
        fit_content.clicked.connect(self.fit_content)
        action_row.addWidget(fit_content)
        fit = QPushButton("Fit cell")
        fit.setToolTip("Show the whole 32×32 permission cell.")
        fit.clicked.connect(self.fit_map)
        action_row.addWidget(fit)
        center.addLayout(title_row)
        center.addLayout(action_row)
        neighbor_row = QHBoxLayout()
        neighbor_row.setContentsMargins(12, 4, 12, 0)
        neighbor_row.setSpacing(10)
        self.neighbors_toggle = QCheckBox("Neighboring cells")
        self.neighbors_toggle.setToolTip("Show surrounding cells. In Select / move, click a neighbor to activate it.")
        self.neighbors_toggle.toggled.connect(self._neighbors_changed)
        neighbor_row.addWidget(self.neighbors_toggle)
        self.events_toggle = QCheckBox("Event markers")
        self.events_toggle.setChecked(True)
        self.events_toggle.toggled.connect(self.redraw)
        neighbor_row.addWidget(self.events_toggle)
        self.neighbor_hint = QLabel("Edits stay in the active cell.")
        self.neighbor_hint.setObjectName("muted")
        self.neighbor_hint.setWordWrap(True)
        self.fit_neighbors_button = QPushButton("Fit neighbors")
        self.fit_neighbors_button.clicked.connect(self.fit_neighbors)
        neighbor_row.addWidget(self.fit_neighbors_button)
        center.addLayout(neighbor_row)
        self.neighbor_hint.setContentsMargins(12, 0, 12, 0)
        center.addWidget(self.neighbor_hint)
        tools = QHBoxLayout()
        tools.setContentsMargins(12, 6, 12, 6)
        tools.setSpacing(6)
        self.tool_buttons = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        for key, text, tip in ((SELECT, "Select / move", "Click a model to select it; drag it on the X/Z plane."),
                               (BLOCK, "Block", "Paint the 0x80 blocking flag on the cells you click."),
                               (UNBLOCK, "Unblock", "Clear the 0x80 blocking flag on the cells you click.")):
            button = QPushButton(text)
            button.setObjectName("tool")
            button.setCheckable(True)
            button.setToolTip(tip)
            button.setChecked(key == SELECT)
            button.clicked.connect(lambda _=False, k=key: self.set_tool(k))
            group.addButton(button)
            tools.addWidget(button)
            self.tool_buttons[key] = button
        self.wf_canvas_buttons = []
        for label, index in [('Select group', 0), ('Brush', 1), ('Rectangle', 2), ('Sample', 3)]:
            button = QPushButton(label)
            button.clicked.connect(lambda _=False, i=index: self.wf_tool.setCurrentIndex(i))
            button.setVisible(False); tools.addWidget(button); self.wf_canvas_buttons.append(button)
        tools.addStretch()
        center.addLayout(tools)
        layers = QHBoxLayout()
        layers.setContentsMargins(12, 0, 12, 6)
        layers.setSpacing(10)
        self.toggles = {}
        # Collision starts hidden while selecting so model textures stay readable;
        # the Block/Unblock tools switch it on.
        for name, checked in (("Textures", True), ("Collision", False), ("Placements", True), ("Grid", False)):
            box = QCheckBox(name)
            box.setChecked(checked)
            box.toggled.connect(self.redraw)
            layers.addWidget(box)
            self.toggles[name] = box
        self._select_collision = False
        layers.addStretch()
        center.addLayout(layers)
        self.graphics = QGraphicsScene(self)
        self.grid = SceneView(self)
        self.grid.setScene(self.graphics)
        self.grid.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.grid.setMinimumSize(320, 240)
        center.addWidget(self.grid, 1)
        center.addWidget(self._legend())
        self.help_label = QLabel(f"  {self.HELP[SELECT]}  {self.PAN_HELP}")
        self.help_label.setObjectName("caption")
        self.help_label.setWordWrap(True)
        center.addWidget(self.help_label)
        return middle

    def open_area_authoring(self):
        if self.wf.actions or self.event_pending() or self.scenery_action or self.staged_move() or self.staged_cells:
            self.notice('Apply or cancel the current edit before opening area authoring.', True)
            return
        from .area_ui import AreaEditor
        self.guard(lambda: AreaEditor(self).exec())

    def open_story_authoring(self):
        if self.wf.actions or self.event_pending() or self.scenery_action or self.staged_move() or self.staged_cells:
            self.notice('Apply or cancel the current edit before opening characters and events.', True)
            return
        from .story_ui import StoryEditor
        self.guard(lambda: StoryEditor(self).exec())

    def open_checkpoints(self):
        if self.wf.actions or self.event_pending() or self.scenery_action or self.staged_move() or self.staged_cells:
            self.notice('Apply or cancel the current edit before managing checkpoints.', True)
            return
        from .recovery_ui import CheckpointsDialog
        self.guard(lambda: CheckpointsDialog(self.project, self, on_changed=lambda: self.load_context(
            self.header, list(self.cell) if hasattr(self, 'cell') else [0, 0])).exec())

    def open_browser(self):
        from .workspace_ui import ProjectBrowser
        self.browser = self.guard(lambda: ProjectBrowser(self))
        if self.browser:
            self.browser.show()

    def open_world(self):
        if self.wf.actions or self.event_pending() or self.scenery_action or self.staged_move() or self.staged_cells:
            self.notice('Apply or cancel the current edit before opening the world editor.', True)
            return
        from .world_ui import WorldEditor
        self.guard(lambda: WorldEditor(self).exec())

    def open_props(self):
        if self.wf.actions or self.event_pending() or self.scenery_action or self.staged_move() or self.staged_cells:
            self.notice('Apply or cancel the current edit before opening custom props.', True)
            return
        from .props_ui import PropEditor
        self.guard(lambda: PropEditor(self).exec())

    def open_gameplay(self):
        if self.wf.actions or self.event_pending() or self.scenery_action or self.staged_move() or self.staged_cells:
            self.notice('Apply or cancel the current edit before opening teams and encounters.', True)
            return
        from .gameplay_ui import GameplayEditor
        self.guard(lambda: GameplayEditor(self.project, self, header=self.header).exec())

    def _inspector(self):
        right = QWidget()
        side = QVBoxLayout(right)
        side.setSpacing(6)
        side.addWidget(self.heading("SCENERY"))
        self.scenery_controls(side)
        side.addWidget(self.heading("SELECTED OBJECT"))
        top = QHBoxLayout()
        self.thumbnail = QLabel("")
        self.thumbnail.setFixedSize(112, 96)
        self.thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumbnail.setWordWrap(True)
        self.thumbnail.setStyleSheet("background: #182229; border: 1px solid #37444d; border-radius: 5px;"
                                     " font-size: 10px; color: #93a4ad;")
        top.addWidget(self.thumbnail)
        names = QVBoxLayout()
        self.object_label = QLabel("No placement selected")
        self.object_label.setStyleSheet("font-size: 16px; font-weight: 600;")
        self.object_label.setWordWrap(True)
        names.addWidget(self.object_label)
        self.object_detail = QLabel("Click a model in the scene or the list below.")
        self.object_detail.setObjectName("muted")
        self.object_detail.setWordWrap(True)
        names.addWidget(self.object_detail)
        names.addStretch()
        top.addLayout(names, 1)
        side.addLayout(top)
        form = QFormLayout()
        form.setContentsMargins(0, 4, 0, 0)
        self.x_input, self.z_input = AnchorSpinBox(), AnchorSpinBox()
        self.x_input.valueChanged.connect(self._anchor_edited)
        self.z_input.valueChanged.connect(self._anchor_edited)
        form.addRow("Anchor X", self.x_input)
        form.addRow("Anchor Z", self.z_input)
        side.addLayout(form)
        self.height_label = QLabel("Record height and BDHC terrain stay unchanged.")
        self.height_label.setObjectName("muted")
        self.height_label.setWordWrap(True)
        side.addWidget(self.height_label)
        side.addWidget(self.heading("PENDING TRANSACTION"))
        self.pending_label = QLabel("Nothing staged.")
        self.pending_label.setWordWrap(True)
        side.addWidget(self.pending_label)
        # The one warning that matters at the moment a move is applied.
        self.move_warning = QLabel("Moves only the model: its door, sign text and scripts stay where they are "
                                   "(MAP-AUTH-001).")
        self.move_warning.setWordWrap(True)
        self.move_warning.setStyleSheet("color: #e5bf7e; font-size: 11px;")
        side.addWidget(self.move_warning)
        self.align_sign = QCheckBox("Align this town sign’s text interaction")
        self.align_sign.setToolTip("Explicitly align New Bark's identified sign event; keep its existing text and script.")
        self.align_sign.toggled.connect(self._describe_pending)
        self.align_sign.setVisible(False)
        side.addWidget(self.align_sign)
        buttons = QHBoxLayout()
        preview = QPushButton("Preview")
        preview.clicked.connect(self.preview)
        buttons.addWidget(preview)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setToolTip("Discard the staged move and staged cells. Nothing is written.")
        self.cancel_button.clicked.connect(self.cancel_staged)
        buttons.addWidget(self.cancel_button)
        self.apply_button = QPushButton("Apply")
        self.apply_button.setObjectName("primary")
        self.apply_button.setToolTip("Write the staged move and staged cells as one project transaction.")
        self.apply_button.clicked.connect(self.apply_transaction)
        buttons.addWidget(self.apply_button)
        side.addLayout(buttons)
        side.addWidget(self.heading("PLACEMENTS"))
        self.placement_list = QListWidget()
        self.placement_list.setMinimumHeight(110)
        self.placement_list.setMaximumHeight(170)
        self.placement_list.currentItemChanged.connect(self._placement_selected)
        side.addWidget(self.placement_list)
        side.addWidget(self.heading("SELECTED CELLS (DOUBLE-CLICK)"))
        self.move_collision = QCheckBox("Move/copy selected collision cells with it")
        self.move_collision.toggled.connect(self._collision_selection_changed)
        side.addWidget(self.move_collision)
        selected_row = QHBoxLayout()
        for text, handler, tip in (
                ("Block", lambda: self.paint_selection(True),
                 "Stage the 0x80 blocking flag on every double-click selected cell. Type bits are kept."),
                ("Unblock", lambda: self.paint_selection(False),
                 "Stage clearing 0x80 on every selected cell. A water or void type still blocks walking."),
                ("Clear", self.clear_selection, "Drop the cell selection. Staged changes are kept.")):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(handler)
            selected_row.addWidget(button)
        side.addLayout(selected_row)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMinimumHeight(120)
        side.addWidget(self.detail, 1)
        history = QHBoxLayout()
        self.undo_button = QPushButton("Undo last edit")
        self.undo_button.clicked.connect(self.undo)
        history.addWidget(self.undo_button)
        self.export_button = QPushButton("Export ROM…")
        self.export_button.clicked.connect(self.export_dialog)
        history.addWidget(self.export_button)
        side.addLayout(history)
        self.revision_label = QLabel("")
        self.revision_label.setObjectName("muted")
        self.revision_label.setWordWrap(True)
        side.addWidget(self.revision_label)
        scope = QLabel("Edits are explicit choices: collision ownership and native runtime behaviour are not proven.")
        scope.setObjectName("muted")
        scope.setWordWrap(True)
        side.addWidget(scope)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(290)
        scroll.setMaximumWidth(380)
        scroll.setWidget(right)
        return scroll

    def _legend(self):
        """One wrapping line of swatches, so the legend never widens the window."""
        entries = []
        for text, colour in LEGEND:
            solid = QColor(colour)
            solid.setAlpha(255)
            entries.append(f"<span style='color:{solid.name()}'>■</span>&nbsp;{text}")
        legend = QLabel("&nbsp;&nbsp; ".join(entries))
        legend.setTextFormat(Qt.TextFormat.RichText)
        legend.setWordWrap(True)
        legend.setObjectName("muted")
        legend.setContentsMargins(14, 4, 14, 4)
        legend.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        return legend

    def heading(self, text):
        label = QLabel(text)
        label.setObjectName("section")
        return label

    def notice(self, text, error=False):
        self.message.setText(text)
        self.message.setStyleSheet("color: #f4ac9c;" if error else "color: #a6dbca;")

    def guard(self, operation):
        try:
            return operation()
        except (EditorError, OSError, ValueError, KeyError) as exc:
            self.notice(str(exc), True)
            return None

    # ---- context selection ---------------------------------------------------

    def reload_cells(self, page=0):
        page = max(0, page)
        listing = self.guard(lambda: self.project.contexts(
            matrix=self.matrix_input.value(), search=self.search_input.text().strip() or None,
            limit=PAGE, offset=page * PAGE))
        if not listing:
            return
        self.page = page
        self.listing = listing
        self.cell_list.blockSignals(True)
        self.cell_list.clear()
        for cell in listing["cells"]:
            header = cell["header"]
            item = QListWidgetItem(f"{cell['cell'][0]},{cell['cell'][1]}  member {cell['map_member']}  "
                                   + (f"h{header}  {cell['name']}" if header is not None
                                      else "(no header section)"))
            item.setData(Qt.ItemDataRole.UserRole, (header, cell["cell"]))
            self.cell_list.addItem(item)
        self.cell_list.blockSignals(False)
        first = listing["offset"] + 1 if listing["returned"] else 0
        self.page_label.setText(f"{first}–{listing['offset'] + listing['returned']} of {listing['total']}")
        self.previous_button.setEnabled(page > 0)
        self.next_button.setEnabled(listing["offset"] + listing["returned"] < listing["total"])
        self.notice(f"Matrix {listing['matrix']} '{listing['matrix_name']}': {listing['total']} cell(s)"
                    + (f" matching '{listing['search']}'" if listing["search"] else "")
                    + (". " + listing["header_required"] if listing["header_required"] else "."))

    def open_header(self):
        """Header-first selection, which also works for headerless matrices."""
        header = self.header_input.value()
        listing = self.guard(lambda: self.project.contexts(header=header, limit=PAGE))
        if not listing:
            return
        self.matrix_input.setValue(listing["matrix"])
        self.reload_cells(0)
        if listing["total"] == 1:
            self.load_context(header, listing["cells"][0]["cell"])
        else:
            self.notice(f"Header {header} covers {listing['total']} cell(s) of matrix "
                        f"{listing['matrix']}; choose one in the list.")

    def _cell_selected(self, item, _):
        if item:
            header, cell = item.data(Qt.ItemDataRole.UserRole)
            self.load_context(header if header is not None else self.header_input.value(), list(cell))

    def load_context(self, header, cell, preserve_view=False):
        """Open a context. Staged edits never survive a context change."""
        if self.wf.actions or self.scenery_action or self.event_pending():
            self.notice("Apply or cancel the pending edit before changing maps.", True)
            return
        center = None
        if preserve_view and self.view_data:
            point = self.grid.mapToScene(self.grid.viewport().rect().center())
            ox, oz = self.view_data["context"]["origin"]
            center = (ox + point.x() / TILE, oz + point.y() / TILE)
        view = self.guard(lambda: self.project.map_view(header=header, cell=cell))
        if not view:
            return
        self.header, self.cell = header, cell
        self.header_input.setValue(header)
        self.view_data = view
        self._clear_staged()
        self.detail.setPlainText("")
        self._highlight_context(header, cell)
        self.refresh()
        if center:
            ox, oz = self.view_data["context"]["origin"]
            self.grid.centerOn((center[0] - ox) * TILE, (center[1] - oz) * TILE)
        else:
            self.fit_content()
            # Fit again once the window has its real size (the first fit may run hidden).
            QTimer.singleShot(0, self.fit_content)
        if self.view_data and self.view_data["context"]["map_member"] == view["context"]["map_member"]:
            unsupported = len(self.scene_data["unsupported"]) if self.scene_data else 0
            self.notice(f"Opened {self.title_label.text()} · {len(self.view_data['placements'])} placement(s)"
                        + (f" · {unsupported} unsupported resource(s) listed at left" if unsupported else "")
                        + (f" · scene unavailable: {self.scene_error}" if self.scene_error else ""),
                        error=bool(self.scene_error))

    def _select_listed(self, header, cell):
        for row in range(self.cell_list.count()):
            item = self.cell_list.item(row)
            item_header, item_cell = item.data(Qt.ItemDataRole.UserRole)
            if list(item_cell) == list(cell) and item_header in (header, None):
                self.cell_list.blockSignals(True)
                self.cell_list.setCurrentItem(item)
                self.cell_list.scrollToItem(item)
                self.cell_list.blockSignals(False)
                return True
        return False

    def _highlight_context(self, header, cell):
        """Keep the browser on the open context: its matrix, its page and its row."""
        matrix = self.view_data["matrix"]["id"]
        if getattr(self, 'listing', {}).get('matrix') == matrix and self._select_listed(header, cell):
            return
        search = self.search_input.text().strip() or None
        for term in ([search, None] if search else [None]):
            offset = 0
            while True:
                listing = self.guard(lambda: self.project.contexts(matrix=matrix, search=term,
                                                                   limit=400, offset=offset))
                if not listing:
                    return
                index = next((i for i, c in enumerate(listing["cells"])
                              if c["cell"] == list(cell) and c["header"] in (header, None)), None)
                if index is not None:
                    if term != search:
                        self.search_input.setText("")
                    self.matrix_input.setValue(matrix)
                    self.reload_cells((offset + index) // PAGE)
                    self._select_listed(header, cell)
                    return
                offset += 400
                if offset >= listing["total"]:
                    break

    def _clear_staged(self):
        if hasattr(self, 'wf') and not self.wf.actions:
            from .workflow_session import WorkflowSession
            self.wf = WorkflowSession(self.project)
            self.wf_ids.clear(); self.wf_gesture = None
        self.selected_event = None
        self.event_detail.clear()
        self.scenery_action = None
        self.scenery_scenes = {}
        self.scenery_plan_cells = []
        self.align_sign.blockSignals(True)
        self.align_sign.setChecked(False)
        self.align_sign.blockSignals(False)
        self.selected_cells = set()
        self.staged_cells = {}
        self.selected_slot = None
        # No selection means no anchors to show; never keep another object's values.
        self.x_input.set_anchor(0)
        self.z_input.set_anchor(0)

    def refresh(self):
        if not self.wf.actions and self.wf.revision != self.project.doc['revision']:
            from .workflow_session import WorkflowSession
            self.wf = WorkflowSession(self.project)
        saved = self.project
        if self.wf.actions and not self.wf_before.isChecked():
            self.project = self.wf.preview
        try:
            self._refresh_display()
        finally:
            self.project = saved
        self.wf_sync()

    def _refresh_display(self):
        """Reload the composed view and scene for the current context."""
        view = self.guard(lambda: self.project.map_view(header=self.header, cell=self.cell))
        if not view:
            return
        old_matrix = self.view_data['matrix']['id'] if self.view_data else None
        self.view_data = view
        if old_matrix is not None and old_matrix != view['matrix']['id']:
            self._highlight_context(self.header, self.cell)
        self.sign_data = self.guard(lambda: self.project.map_sign(header=self.header, cell=self.cell)) or {"supported": False}
        self.scene_data = self.scene_error = None
        try:
            self.scene_data = self.project.map_scene(header=self.header, cell=self.cell)
        except (EditorError, OSError, ValueError) as exc:
            # A scene that cannot be rendered must not disable permission editing.
            self.scene_error = str(exc)
        self.neighborhood_data = None
        if self.neighbors_toggle.isChecked():
            self.neighborhood_data = self.guard(lambda: self.project.map_neighborhood(header=self.header, cell=self.cell))
        self.fit_neighbors_button.setEnabled(bool(self.neighborhood_data))
        context, area = view["context"], view["area_data"]
        title = self.scene_data["title"] if self.scene_data else None
        self.title_label.setText(title or context["name"])
        self.subtitle_label.setText(
            (f"{context['name']} · " if title else "")
            + f"header {view['header']['id']} · matrix {view['matrix']['id']} cell "
            f"{view['cell']['x']},{view['cell']['y']} · map member {context['map_member']} · "
            f"{area['area_type_name']}")
        self.context_label.setText(
            f"{context['name']} · {context['id']}\nmap member {context['map_member']} of {context['map_archive']}\n"
            f"origin {context['origin']} · {area['area_type_name']} · event file {context['event_member']}\n"
            f"member reused in cells {context['shared_cells']}")
        self._describe_resources()
        self.update_destinations()
        if self.selected_slot is None:
            saved = self.project.doc.get("map_selection")
            if saved and "object_id" in saved and saved["header"] == self.header and saved["cell"] == self.cell:
                selected = next((p for p in view["placements"] if p["object_id"] == saved["object_id"]), None)
                if selected:
                    self.selected_slot = selected["slot"]
                    self.x_input.set_anchor(selected["position"]["x"])
                    self.z_input.set_anchor(selected["position"]["z"])
        self.placement_list.blockSignals(True)
        self.placement_list.clear()
        for placement in view["placements"]:
            mark = " •" if placement["changed"] else ""
            lock = "" if placement["editable"] else "  (locked)"
            scene = self._scene_placement(placement["slot"])
            name = scene["display"] if scene else f"model {placement['model_id']}"
            flag = "" if not scene or scene["status"] == "ok" else "  ⚠"
            item = QListWidgetItem(f"{placement['key']}  {name}  "
                                   f"({placement['position']['x']:g}, {placement['position']['z']:g}){mark}{lock}{flag}")
            item.setData(Qt.ItemDataRole.UserRole, placement["slot"])
            self.placement_list.addItem(item)
            if placement["slot"] == self.selected_slot:
                self.placement_list.setCurrentItem(item)
        self.placement_list.blockSignals(False)
        self.revision_label.setText(
            f"Revision {self.project.doc['revision']} · {len(self.project.doc['map_edits'])} "
            f"authored transaction(s) · {len(view['changed_permission_cells'])} changed cell(s) here")
        self.undo_button.setEnabled(bool(self.project.doc["history"]))
        self.refresh_events()
        self.redraw()
        if self.selected_slot is not None and self.selected_event is None:
            self._select_placement(self.selected_slot)
        self._describe_pending()

    def _describe_resources(self):
        if not self.scene_data:
            self.resource_label.setText("Textured scene unavailable for this context.")
            self.unsupported_label.setText(self.scene_error or "")
            return
        resources, terrain = self.scene_data["resources"], self.scene_data["resources"]["terrain"]
        lines = [f"terrain {terrain.get('name', '—')} · {terrain.get('triangles', 0)} triangles"
                 f" ({terrain['archive']} member {terrain['member']})",
                 f"map tileset {resources['map_tileset']['archive']} member {resources['map_tileset']['member']}",
                 f"building tileset {resources['building_tileset']['archive']} "
                 f"member {resources['building_tileset']['member']}",
                 f"building models {resources['building_models']['archive']} · "
                 f"{len(self.scene_data['models'])} distinct model(s)"]
        self.resource_label.setText("\n".join(lines))
        unsupported = self.scene_data["unsupported"]
        self.unsupported_label.setText(
            "" if not unsupported else
            f"{len(unsupported)} unsupported resource(s):\n"
            + "\n".join(f"· {u['resource']} {u.get('name') or u.get('key') or u.get('member')}: {u['reason']}"
                        for u in unsupported[:4]))

    def _scene_placement(self, slot):
        if not self.scene_data:
            return None
        return next((p for p in self.scene_data["placements"] if p["slot"] == slot), None)

    # ---- scene ---------------------------------------------------------------

    HELP = {SELECT: "Select / move: click a model and drag it on the X/Z plane, then Apply.",
            BLOCK: "Block: click or drag over cells to stage the 0x80 flag. Type bits are kept.",
            UNBLOCK: "Unblock: click or drag to stage clearing 0x80. A water or void type still blocks."}
    PAN_HELP = "Pan: middle-drag, Space+drag or drag open ground · Zoom: wheel · Double-click selects a cell."

    def set_tool(self, tool):
        if self.event_pending() and tool != SELECT:
            for key, button in self.tool_buttons.items():
                button.setChecked(key == self.tool)
            self.notice("Apply or cancel the event edit before painting collision.", True)
            return
        if tool != SELECT:
            self.selected_event = None
            self.populate_event()
            self.inspector_tabs.setCurrentIndex(0)
        painting_before = self.tool != SELECT
        self.tool = tool
        for key, button in self.tool_buttons.items():
            button.setChecked(key == tool)
        collision = self.toggles["Collision"]
        if tool == SELECT and painting_before:
            collision.setChecked(self._select_collision)
        elif tool != SELECT and not painting_before:
            # Cells are painted against the grid, so show it; restore the choice later.
            self._select_collision = collision.isChecked()
            collision.setChecked(True)
        self.grid.update_cursor()
        self.help_label.setText(f"  {self.HELP[tool]}  {self.PAN_HELP}")
        self.notice(self.HELP[tool])

    def redraw(self, *_):
        if not self.view_data:
            return
        self.graphics.clear()
        side = 32 * TILE
        self.graphics.setSceneRect(0, 0, side, side)
        ox, oz = self.view_data["context"]["origin"]
        self._draw_neighbors(ox, oz)
        displayed_scene = self.scenery_scenes.get((self.header, tuple(self.cell)), self.scene_data)
        if displayed_scene and self.toggles["Textures"].isChecked():
            pixmap = QPixmap(displayed_scene["image"])
            if not pixmap.isNull():
                item = self.graphics.addPixmap(pixmap)
                item.setScale(side / pixmap.width())
                item.setZValue(-10)
        rows = self.view_data["permissions"]["rows"]
        changed = {(c["x"], c["z"]) for c in self.view_data["changed_permission_cells"]}
        faint = QPen(QColor(240, 255, 250, 28), .5)
        edge = QPen(BLOCKED_EDGE, 1)
        collision = self.toggles["Collision"].isChecked()
        grid = self.toggles["Grid"].isChecked()
        for lz, row in enumerate(rows):
            for lx, value in enumerate(row):
                pair = bytes.fromhex(value)
                blocked = bool(pair[1] & 128) or pair[0] in (16, 21)
                if collision and blocked:
                    self.graphics.addRect(lx * TILE + .5, lz * TILE + .5, TILE - 1, TILE - 1, edge, QBrush(BLOCKED))
                if grid:
                    self.graphics.addRect(lx * TILE, lz * TILE, TILE, TILE, faint, Qt.BrushStyle.NoBrush)
                if (ox + lx, oz + lz) in changed:
                    self.graphics.addRect(lx * TILE + 2, lz * TILE + 2, TILE - 4, TILE - 4,
                                          QPen(CHANGED, 2), Qt.BrushStyle.NoBrush)
                if (ox + lx, oz + lz) in self.selected_cells:
                    self.graphics.addRect(lx * TILE, lz * TILE, TILE, TILE, QPen(QColor("#85dbc0"), 2),
                                          QBrush(SELECTED))
        for (x, z), change in self.staged_cells.items():
            blocked = bool(bytes.fromhex(change["after"])[1] & 128)
            mark = self.graphics.addRect((x - ox) * TILE + 1, (z - oz) * TILE + 1, TILE - 2, TILE - 2,
                                         QPen(MOVED if not blocked else UNSUPPORTED, 2),
                                         QBrush(STAGED_BLOCK if blocked else STAGED_OPEN))
            mark.setZValue(4)
            mark.setToolTip(f"staged {x},{z}: {change['before']} → {change['after']}")
        if self.toggles["Placements"].isChecked():
            self._draw_placements(ox, oz)
        self._draw_staged_move(ox, oz)
        self.draw_scenery_action(ox, oz)
        self.draw_events(ox, oz)
        self.wf_draw()
        if self.neighborhood_data:
            pen = QPen(MOVED, 3)
            pen.setCosmetic(True)
            border = self.graphics.addRect(0, 0, side, side, pen, Qt.BrushStyle.NoBrush)
            border.setZValue(8)
            label = self.graphics.addText(f"ACTIVE · {self.title_label.text()} · cell {self.cell[0]},{self.cell[1]}")
            label.setDefaultTextColor(MOVED)
            label.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
            label.setFont(QFont("Arial", 10, QFont.Weight.Bold))
            label.setTextWidth(195)
            label.setPos(4, 2)
            label.setZValue(9)
            self._label_background(label)

    def _neighbors_changed(self, *_):
        if not self.view_data:
            return
        self.refresh()
        self.fit_neighbors() if self.neighbors_toggle.isChecked() else self.fit_content()
        if not self.neighbors_toggle.isChecked():
            self.neighbor_hint.setText("Edits stay in the active cell.")
            self.neighbor_hint.setToolTip("")

    def _label_background(self, text):
        backing = self.graphics.addRect(text.boundingRect(), QPen(Qt.PenStyle.NoPen),
                                        QBrush(QColor(20, 27, 32, 225)))
        backing.setParentItem(text)
        backing.setZValue(-1)
        backing.setFlag(QGraphicsItem.GraphicsItemFlag.ItemStacksBehindParent)

    def _draw_neighbors(self, ox, oz):
        if not self.neighborhood_data:
            return
        side = 32 * TILE
        bounds = QRectF(0, 0, side, side)
        issues = []
        for entry in self.neighborhood_data["cells"]:
            if entry["active"]:
                continue
            x, z = ((entry["origin"][0] - ox) * TILE, (entry["origin"][1] - oz) * TILE)
            rect = QRectF(x, z, side, side)
            bounds = bounds.united(rect)
            scene = self.scenery_scenes.get((entry.get("header"), tuple(entry["cell"])), entry.get("scene"))
            if scene and self.toggles["Textures"].isChecked() and scene.get("image"):
                pixmap = QPixmap(scene["image"])
                if not pixmap.isNull():
                    item = self.graphics.addPixmap(pixmap)
                    item.setPos(x, z)
                    item.setScale(side / pixmap.width())
                    item.setOpacity(.72)
                    item.setZValue(-11)
            color = QColor("#71858d") if entry["status"] == "ok" else QColor("#a36d5b")
            pen = QPen(color, 1.5)
            pen.setCosmetic(True)
            outline = self.graphics.addRect(rect, pen, Qt.BrushStyle.NoBrush)
            outline.setZValue(6)
            label = (scene.get("title") if scene else None) or entry.get("context", {}).get("name") or entry["status"]
            label += f" · cell {entry['cell'][0]},{entry['cell'][1]}"
            if entry["map_member"] is not None:
                label += f" · member {entry['map_member']}"
            reason = entry.get("reason") or entry.get("scene_error")
            if scene and scene["unsupported"]:
                reason = "; ".join(u["reason"] for u in scene["unsupported"])
            if reason:
                label += " · preview warning"
                issues.append(f"Cell {entry['cell']}: {reason}")
            text = self.graphics.addText(label)
            text.setDefaultTextColor(color.lighter(140))
            text.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
            text.setFont(QFont("Arial", 9))
            text.setPos(x + 4, z + 2)
            text.setTextWidth(195)
            text.setZValue(7)
            self._label_background(text)
            outline.setToolTip(reason or "Select / move: click to activate this cell; apply or cancel pending edits first.")
        self.graphics.setSceneRect(bounds)
        self.neighbor_hint.setText("Click a neighbor in Select / move to edit it. "
                                   + (f"{len(issues)} preview warning(s); hover cell borders." if issues else
                                      "Bright border = active cell."))
        self.neighbor_hint.setToolTip("\n".join(issues) or self.neighborhood_data["scope"])

    def fit_neighbors(self):
        self._fit_mode = 'neighbors'
        self.grid.fitInView(self.graphics.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def activate_neighbor(self, tile_x, tile_z):
        """Consume neighbor clicks without silently dropping a pending transaction."""
        if not self.neighborhood_data or (0 <= tile_x < 32 and 0 <= tile_z < 32):
            return False
        cell = [self.cell[0] + int(tile_x // 32), self.cell[1] + int(tile_z // 32)]
        entry = next((c for c in self.neighborhood_data["cells"] if c["cell"] == cell), None)
        if not entry or entry["status"] != "ok":
            self.notice("This neighboring cell is empty or unsupported; no edit was staged.", True)
            return True
        if self.event_pending() or self.scenery_action or self.staged_move() or self.staged_cells or self.selected_cells or self.align_sign.isChecked():
            self.notice("Apply or cancel the current selection/transaction before activating a neighboring cell.", True)
            return True
        self.load_context(entry["header"], cell, preserve_view=True)
        return True

    def _draw_placements(self, ox, oz):
        for placement in self.view_data["placements"]:
            scene = self._scene_placement(placement["slot"])
            x = (placement["position"]["x"] - ox) * TILE
            z = (placement["position"]["z"] - oz) * TILE
            selected = placement["slot"] == self.selected_slot
            colour = MOVED if placement["changed"] else PLACEMENT
            if scene and scene["status"] != "ok":
                colour = UNSUPPORTED
            if scene and scene["footprint"]:
                x0, z0, x1, z1 = scene["footprint"]
                box = self.graphics.addRect((x0 - ox) * TILE, (z0 - oz) * TILE,
                                            (x1 - x0) * TILE, (z1 - z0) * TILE,
                                            QPen(colour, 2 if selected else 1,
                                                 Qt.PenStyle.SolidLine if selected else Qt.PenStyle.DotLine),
                                            QBrush(QColor(229, 191, 126, 30)) if selected else Qt.BrushStyle.NoBrush)
                box.setZValue(5)
            mark = self.graphics.addEllipse(x - 5, z - 5, 10, 10, QPen(colour, 2), QBrush(QColor(24, 34, 41)))
            mark.setZValue(6)
            name = scene["display"] if scene else f"model {placement['model_id']}"
            mark.setToolTip(f"{placement['key']} · {name}\n"
                            f"anchor {placement['position']['x']:g}, {placement['position']['z']:g}"
                            + (f"\nunsupported: {scene['reason']}" if scene and scene["status"] != "ok" else "")
                            + ("" if placement["editable"] else f"\nlocked: {placement['locked_by']}"))
            if selected:
                ring = self.graphics.addEllipse(x - 11, z - 11, 22, 22, QPen(QColor("#85dbc0"), 2),
                                                Qt.BrushStyle.NoBrush)
                ring.setZValue(7)

    def _draw_staged_move(self, ox, oz):
        move = self.staged_move()
        if not move:
            return
        before, after = move["before"], move["after"]
        scene = self._scene_placement(self.selected_slot)
        if scene and scene["footprint"]:
            x0, z0, x1, z1 = scene["footprint"]
            ghost = self.graphics.addRect((x0 - ox + after["x"] - before["x"]) * TILE,
                                          (z0 - oz + after["z"] - before["z"]) * TILE,
                                          (x1 - x0) * TILE, (z1 - z0) * TILE,
                                          QPen(MOVED, 2, Qt.PenStyle.DashLine), QBrush(GHOST))
            ghost.setZValue(8)
        old = self.graphics.addEllipse((before["x"] - ox) * TILE - 7, (before["z"] - oz) * TILE - 7, 14, 14,
                                       QPen(PLACEMENT, 2, Qt.PenStyle.DashLine), Qt.BrushStyle.NoBrush)
        old.setZValue(9)
        old.setToolTip(f"current record position {before['x']:g}, {before['z']:g}")
        line = self.graphics.addLine((before["x"] - ox) * TILE, (before["z"] - oz) * TILE,
                                     (after["x"] - ox) * TILE, (after["z"] - oz) * TILE, QPen(MOVED, 2))
        line.setZValue(9)
        new = self.graphics.addEllipse((after["x"] - ox) * TILE - 7, (after["z"] - oz) * TILE - 7, 14, 14,
                                       QPen(MOVED, 3), QBrush(QColor(24, 34, 41)))
        new.setZValue(10)
        new.setToolTip(f"staged position {after['x']:g}, {after['z']:g}")
        # Readable at any zoom: the labels ignore the view transform and are offset
        # in screen pixels from the markers they name.
        self.move_labels = []
        for text, point, colour, dy in ((f"old {before['x']:g}, {before['z']:g}", before, PLACEMENT, -28),
                                        (f"new {after['x']:g}, {after['z']:g}", after, MOVED, 12)):
            label = self.graphics.addSimpleText(text)
            font = QFont()
            font.setPixelSize(12)
            font.setBold(True)
            label.setFont(font)
            label.setBrush(QBrush(colour))
            label.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
            label.setPos((point["x"] - ox) * TILE, (point["z"] - oz) * TILE)
            label.setTransform(label.transform().translate(10, dy))
            label.setZValue(11)
            backing = self.graphics.addRect(label.boundingRect().adjusted(-3, -1, 3, 1),
                                            QPen(Qt.PenStyle.NoPen), QBrush(QColor(16, 22, 27, 210)))
            backing.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
            backing.setPos(label.pos())
            backing.setTransform(label.transform())
            backing.setZValue(10.5)
            self.move_labels.append(label)

    def pick_placement(self, tile_x, tile_z):
        """Scene pick: the smallest solid drawn extent containing the point, else the nearest anchor.

        Ambient geometry (an extent over a quarter of the cell, such as New Bark's wind
        streaks) is not a solid hit: open ground inside its box stays open ground. Its
        anchor marker and the placement list still select it.
        """
        if not self.view_data:
            return None
        ox, oz = self.view_data["context"]["origin"]
        x, z = ox + tile_x, oz + tile_z
        hits = []
        for placement in self.view_data["placements"]:
            scene = self._scene_placement(placement["slot"])
            if not scene or not scene["footprint"] or scene["ambient"]:
                continue
            box = scene["footprint"]
            if box[0] <= x <= box[2] and box[1] <= z <= box[3]:
                hits.append(((box[2] - box[0]) * (box[3] - box[1]), placement["slot"]))
        if hits:
            return min(hits)[1]
        near = [((placement["position"]["x"] - x) ** 2 + (placement["position"]["z"] - z) ** 2, placement["slot"])
                for placement in self.view_data["placements"]]
        closest = min(near) if near else None
        return closest[1] if closest and closest[0] <= 0.36 else None

    def refit_scene(self):
        self._fit_pending = False
        if not getattr(self, 'view_data', None):
            return
        mode = getattr(self, '_fit_mode', None)
        if mode == 'content':
            self.fit_content()
        elif mode == 'cell':
            self.fit_map()
        elif mode == 'neighbors':
            self.fit_neighbors()

    def fit_map(self):
        self._fit_mode = 'cell'
        self.grid.fitInView(QRectF(0, 0, 32 * TILE, 32 * TILE), Qt.AspectRatioMode.KeepAspectRatio)

    def fit_content(self):
        """Fit the drawn terrain/room content inside the cell, padded by one tile.

        Interiors use only part of their 32x32 cell; the whole cell stays one click
        away with Fit cell, and tile/pixel conversion is unchanged.
        """
        self._fit_mode = 'content'
        content = self.scene_data and self.scene_data.get("content")
        if not content or not self.view_data:
            return self.fit_map()
        ox, oz = self.view_data["context"]["origin"]
        x0, z0, x1, z1 = content["tiles"]
        rect = QRectF((x0 - ox - 1) * TILE, (z0 - oz - 1) * TILE, (x1 - x0 + 2) * TILE, (z1 - z0 + 2) * TILE)
        self.grid.fitInView(rect.intersected(QRectF(-TILE, -TILE, 34 * TILE, 34 * TILE)),
                            Qt.AspectRatioMode.KeepAspectRatio)

    def focus_tile(self, x, z, tiles=12):
        """Zoom to a window of tiles around a global tile, for review captures."""
        self._fit_mode = None
        ox, oz = self.view_data["context"]["origin"]
        self.grid.fitInView(QRectF((x - ox - tiles / 2) * TILE, (z - oz - tiles / 2) * TILE,
                                   tiles * TILE, tiles * TILE), Qt.AspectRatioMode.KeepAspectRatio)

    # ---- cells ---------------------------------------------------------------

    def _collision_selection_changed(self, *_):
        if self.scenery_action:
            self.scenery_scenes = {}
            self.scenery_plan_cells = []
        self.redraw()
        self._describe_pending()

    def toggle_cell(self, lx, lz):
        if not self.view_data or not (0 <= lx < 32 and 0 <= lz < 32):
            return
        ox, oz = self.view_data["context"]["origin"]
        cell = (ox + lx, oz + lz)
        self.selected_cells ^= {cell}
        self._collision_selection_changed()
        value = self.view_data["permissions"]["rows"][lz][lx]
        self.notice(f"{len(self.selected_cells)} cell(s) selected; last {cell} = {value}")

    def clear_selection(self):
        self.selected_cells = set()
        self._collision_selection_changed()
        self.notice("Selection cleared.")

    def _stage_flag(self, x, z, blocked):
        """Stage the requested final blocking flag of one cell against its saved pair.

        The stage always reflects the latest action: a value equal to the saved pair
        removes the stage, so painting Block then Unblock over the same cells leaves
        nothing staged. The type byte and every non-0x80 bit are kept.
        Returns (changed, before, after).
        """
        ox, oz = self.view_data["context"]["origin"]
        pair = bytes.fromhex(self.view_data["permissions"]["rows"][z - oz][x - ox])
        after = bytes((pair[0], (pair[1] | 128) if blocked else (pair[1] & ~128 & 0xFF)))
        previous = self.staged_cells.get((x, z))
        if after == pair:
            self.staged_cells.pop((x, z), None)
        else:
            self.staged_cells[(x, z)] = {"x": x, "z": z, "before": pair.hex(), "after": after.hex()}
        current = self.staged_cells.get((x, z))
        if previous != current and self.scenery_action:
            self.scenery_scenes = {}
            self.scenery_plan_cells = []
        return previous != current, pair, after

    def paint_tile(self, lx, lz, repeated=False):
        """Stage one explicit cell change under the active Block/Unblock tool."""
        if not self.view_data or self.tool == SELECT:
            return
        if not (0 <= lx < 32 and 0 <= lz < 32):
            self.notice("Painting stays in the active cell. Use Select / move to activate its neighbor.", True)
            return
        ox, oz = self.view_data["context"]["origin"]
        cell = (ox + lx, oz + lz)
        blocked = self.tool == BLOCK
        changed, pair, after = self._stage_flag(cell[0], cell[1], blocked)
        if repeated and not changed:
            return
        self.redraw()
        self._describe_pending()
        if after == pair:
            self.notice(f"Cell {cell[0]},{cell[1]} is {'blocked' if blocked else 'unblocked'} in the saved map "
                        f"({pair.hex()})" + ("; its staged change was removed." if changed else
                                             f"; already {'blocked' if blocked else 'unblocked'}, nothing staged."))
            return
        self.notice(f"Staged {cell[0]},{cell[1]}: {pair.hex()} → {after.hex()} "
                    f"({len(self.staged_cells)} staged cell(s)). Apply writes them in one transaction.")

    def paint_selection(self, blocked):
        """Stage the same explicit change for every double-click selected cell.

        Only the 0x80 blocking flag changes: the terrain type byte and every other
        collision bit are written back unchanged, so a blocking terrain type such as
        water or void still blocks after the flag is cleared.
        """
        if not self.selected_cells:
            self.notice("Select permission cells first (double-click a cell).", True)
            return
        for x, z in sorted(self.selected_cells):
            self._stage_flag(x, z, blocked)
        staged = sum(cell in self.staged_cells for cell in self.selected_cells)
        self.redraw()
        self._describe_pending()
        self.notice(f"{'Block' if blocked else 'Unblock'}: {staged} of {len(self.selected_cells)} selected cell(s) "
                    "now differ from the saved map. Apply writes them in one transaction.")

    # ---- placement + transaction --------------------------------------------

    def _placement_selected(self, item, _):
        if item:
            self._select_placement(item.data(Qt.ItemDataRole.UserRole))

    def select_placement(self, slot):
        """Scene selection, kept in step with the list in both directions."""
        self._select_placement(slot)
        if self.selected_slot == slot and not self.event_pending():
            self.inspector_tabs.setCurrentIndex(0)
        for row in range(self.placement_list.count()):
            item = self.placement_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == slot:
                self.placement_list.blockSignals(True)
                self.placement_list.setCurrentItem(item)
                self.placement_list.blockSignals(False)
        self.redraw()

    def _select_placement(self, slot):
        if self.event_pending():
            self.notice("Apply or cancel the pending event edit before selecting scenery.", True)
            return
        self.selected_event = None
        self.populate_event()
        if self.scenery_action and slot != self.scenery_action["slot"]:
            self.notice("Apply or cancel the pending scenery action before changing objects.", True)
            return
        placement = next(p for p in self.view_data["placements"] if p["slot"] == slot)
        changing = slot != self.selected_slot
        self.selected_slot = slot
        if changing:
            self.align_sign.blockSignals(True)
            self.align_sign.setChecked(False)
            self.align_sign.blockSignals(False)
            # A new selection never inherits the previous object's staged anchors.
            self.x_input.set_anchor(placement["position"]["x"])
            self.z_input.set_anchor(placement["position"]["z"])
        scene = self._scene_placement(slot)
        self.object_label.setText(scene["display"] if scene else f"model {placement['model_id']}")
        self.height_label.setText(
            f"Height {placement['position']['y']:g} (record word {placement['record']['y']}) and the "
            "BDHC terrain stay unchanged.\n"
            f"Record {placement['record_offset']} · rotation {placement['rotation_raw']} · "
            f"scale {placement['scale_raw']} preserved."
            + ("" if placement["editable"] else f"\nLocked by the {placement['locked_by']}."))
        self._show_object(placement, scene)
        self._describe_pending()

    def _show_object(self, placement, scene):
        lines = [f"{placement['key']} · slot {placement['slot']} · model {placement['model_id']}"]
        if scene:
            lines.append(f"internal name {scene['name']}"
                         + (f" · label from {scene['label_basis']}" if scene["label_basis"] else
                            " · no reviewed label; internal name shown"))
            lines.append(f"stock anchor {scene['stock_position']['x']:g}, {scene['stock_position']['z']:g}"
                         f" · rotation {scene['rotation_degrees']:g}°")
            if scene["status"] != "ok":
                lines.append(f"unsupported: {scene['reason']}")
        self.object_detail.setText("\n".join(lines))
        self.thumbnail.setPixmap(QPixmap())
        self.thumbnail.setText("")
        if not scene or scene["status"] == "unsupported":
            self.thumbnail.setText("No preview:\n" + (scene["reason"] if scene else "scene unavailable"))
            self.thumbnail.setWordWrap(True)
            return
        result = self.guard(lambda: self.project.map_thumbnail(placement["model_id"], header=self.header,
                                                              cell=self.cell))
        if result:
            pixmap = QPixmap(result["image"])
            if not pixmap.isNull():
                self.thumbnail.setPixmap(pixmap.scaled(108, 92, Qt.AspectRatioMode.KeepAspectRatio,
                                                       Qt.TransformationMode.SmoothTransformation))
                return
        self.thumbnail.setText("No preview available")

    def _anchor_edited(self, *_):
        """Typed or dragged anchors update the ghost and the pending summary together."""
        if self.scenery_action:
            self.scenery_scenes = {}
            self.scenery_plan_cells = []
        if self.view_data:
            self.redraw()
            self._describe_pending()

    def drag_placement(self, slot, dx_tiles, dz_tiles):
        """Stage an X/Z drag. Record Y and every other word stay untouched."""
        placement = next(p for p in self.view_data["placements"] if p["slot"] == slot)
        if not placement["editable"]:
            self.notice(f"{placement['key']} is locked by the {placement['locked_by']}.", True)
            return
        base = placement["position"]
        step = lambda value: round(value / SNAP) * SNAP
        self.x_input.setValue(base["x"] + step(dx_tiles))
        self.z_input.setValue(base["z"] + step(dz_tiles))
        self._describe_pending()
        self.redraw()

    def staged_move(self):
        """The pending X/Z change of the selected placement, or None."""
        if self.scenery_action or self.selected_slot is None or not self.view_data:
            return None
        placement = next((p for p in self.view_data["placements"] if p["slot"] == self.selected_slot), None)
        if placement is None:
            return None
        after = {"x": self.x_input.anchor_value(), "z": self.z_input.anchor_value()}
        before = {"x": placement["position"]["x"], "z": placement["position"]["z"]}
        return None if after == before else {"slot": self.selected_slot, "before": before, "after": after}

    def _describe_pending(self):
        move = self.staged_move()
        lines = []
        if self.scenery_action:
            action = self.scenery_action
            lines.append(f"{action['operation'].upper()} · donor/object slot {action['slot']}")
            if action['operation'] != 'delete':
                lines.append(f"place at {self.x_input.anchor_value():g}, {self.z_input.anchor_value():g}")
            if action['destination']:
                lines.append(f"destination cell {action['destination']['cell']}")
            lines.append("Preview renders this proposal; Apply saves it.")
        if move:
            lines.append(f"move {self.view_data['context']['map_member']}:{move['slot']}  "
                         f"({move['before']['x']:g}, {move['before']['z']:g}) → "
                         f"({move['after']['x']:g}, {move['after']['z']:g})")
            lines.append("record height, rotation, scale and every other word unchanged")
        for cell in sorted(self.staged_cells.values(), key=lambda c: (c["z"], c["x"])):
            lines.append(f"cell {cell['x']},{cell['z']}: {cell['before']} → {cell['after']}")
        if self.move_collision.isChecked() and self.selected_cells:
            action = self.scenery_action and self.scenery_action["operation"]
            verb = "copied; source kept" if action in ("add", "duplicate") else "moved with the object"
            lines.append(f"{len(self.selected_cells)} selected blocker(s) {verb}")
        if self.align_sign.isChecked() and self._sign_selected():
            target = [int(self.x_input.anchor_value()), int(self.z_input.anchor_value())]
            lines.append(f"town-sign text: {self.sign_data['position']} → {target}; script and text unchanged")
        self.pending_label.setText("\n".join(lines) if lines else "Nothing staged.")
        self.pending_label.setStyleSheet("color: #e5bf7e;" if lines else "color: #93a4ad;")
        self._update_actions()

    def _update_actions(self):
        """Apply only with something staged; anchors only for an editable selection."""
        placement = None
        if self.view_data and self.selected_slot is not None:
            placement = next((p for p in self.view_data["placements"] if p["slot"] == self.selected_slot), None)
        editable = bool(placement and placement["editable"])
        active = self.scenery_action
        available = editable and not self._sign_selected() and not active
        self.duplicate_button.setEnabled(available)
        self.delete_scenery_button.setEnabled(available)
        self.transfer_button.setEnabled(available and self.destination_box.count() > 0)
        self.add_scenery_button.setEnabled(not active)
        self.destination_box.setEnabled(not active or active["operation"] == "transfer")
        move = self.staged_move()
        sign_selected = self._sign_selected()
        self.align_sign.setVisible(sign_selected)
        staged = bool(active) or bool(move and editable) or bool(self.staged_cells) or (sign_selected and self.align_sign.isChecked())
        self.apply_button.setEnabled(staged)
        self.cancel_button.setEnabled(bool(active) or bool(move) or bool(self.staged_cells) or bool(self.selected_cells) or self.align_sign.isChecked())
        self.x_input.setEnabled(active["operation"] != "delete" if active else editable)
        self.z_input.setEnabled(active["operation"] != "delete" if active else editable)
        self.move_warning.setVisible(placement is not None)
        self.move_warning.setText(
            f"Text interaction is at {self.sign_data['position']}. Tick the alignment option to include it in Apply."
            if sign_selected else "Moves only the model: doors, text interactions and scripts stay where they are.")
        if placement is None and not active:
            self.object_label.setText("No placement selected")
            self.object_detail.setText("Click a model in the scene or the list below.")
            self.thumbnail.setPixmap(QPixmap())
            self.thumbnail.setText("")
            self.height_label.setText("Record height and BDHC terrain stay unchanged.")

    def cancel_staged(self):
        """Discard the staged move and staged cells without writing anything."""
        self.scenery_action = None
        self.scenery_scenes = {}
        self.scenery_plan_cells = []
        self.staged_cells = {}
        self.align_sign.setChecked(False)
        if self.selected_slot is not None:
            placement = next((p for p in self.view_data["placements"] if p["slot"] == self.selected_slot), None)
            if placement:
                self.x_input.set_anchor(placement["position"]["x"])
                self.z_input.set_anchor(placement["position"]["z"])
        self.selected_cells = set()
        self.detail.setPlainText("")
        self.redraw()
        self._describe_pending()
        self.notice("Staged changes discarded. Nothing was written.")

    def _request(self):
        placement = None
        if self.selected_slot is not None:
            placement = {"slot": self.selected_slot,
                         "x": self.x_input.anchor_value(), "z": self.z_input.anchor_value()}
        cells = [{"x": x, "z": z} for x, z in sorted(self.selected_cells)] \
            if self.move_collision.isChecked() else []
        return {"header": self.header, "cell": self.cell, "placement": placement, "move_collision": cells,
                "align_sign": self._sign_selected() and self.align_sign.isChecked()}

    def _sign_selected(self):
        return bool(self.sign_data.get("supported") and self.selected_slot == self.sign_data["placement_slot"])

    def _staged_permissions(self):
        return [dict(self.staged_cells[key]) for key in sorted(self.staged_cells, key=lambda c: (c[1], c[0]))]

    def preview(self):
        if self.scenery_action:
            return self.preview_scenery()
        plan = self.guard(lambda: self.project.plan_map_edit(permissions=self._staged_permissions(),
                                                             **self._request()))
        if plan is None:
            return
        self._show_plan(plan["transaction"], plan["preview"], len(plan["patches"]), saved=False)
        self.notice("Preview only. Nothing was saved.")

    def _show_plan(self, transaction, preview, patch_count, saved):
        if scenery.is_transaction(transaction):
            return self.show_scenery_plan(preview, saved)
        lines = [("SAVED" if saved else "PENDING") + f" · {transaction['label']}",
                 preview["permissions"], ""]
        for change in transaction["placements"]:
            lines.append(f"placement {change['slot']}: "
                         f"({change['before']['x']:g},{change['before']['z']:g}) -> "
                         f"({change['after']['x']:g},{change['after']['z']:g}); "
                         f"height {change['before']['y']:g} unchanged")
        for cell in transaction["permissions"]:
            lines.append(f"cell {cell['x']},{cell['z']} (offset {cell['offset']}): "
                         f"{cell['before']} -> {cell['after']} [{cell['source']}]")
        if transaction.get("sign_interaction"):
            change = transaction["sign_interaction"]
            lines.append(f"Town-sign text interaction: {change['from']} -> {change['to']}; same script {change['script']}")
        lines += ["", f"{patch_count} ROM byte patch(es); every other byte is preserved.",
                  json.dumps(preview["unchanged"], indent=1)]
        self.detail.setPlainText("\n".join(lines))

    def _run(self, permissions=(), move_collision=None, label=None):
        request = self._request()
        if move_collision is not None:
            request["move_collision"] = move_collision
        if permissions:
            request["placement"] = None
            request["move_collision"] = []
        result = self.guard(lambda: self.project.apply_map_edit(
            self.project.doc["revision"], permissions=list(permissions), label=label, **request))
        if result is None:
            self.guard(self.refresh)
            return
        if not result["changed"]:
            self.notice("No byte would change; nothing was saved.")
        else:
            self._show_plan(result["transaction"], result["preview"], result["patch_count"], saved=True)
            self._clear_staged()
            self.notice(f"Saved transaction at revision {result['revision']}. Export a ROM for melonDS checks.")
        self.guard(self.refresh)

    def apply_transaction(self):
        """Commit the staged move and staged cells as one Project transaction."""
        if self.wf.actions:
            return self.wf_guard(self.wf_apply)
        if self.scenery_action:
            return self.apply_scenery()
        staged = self._staged_permissions()
        if self.selected_slot is None and not staged:
            self.notice("Select a placement or stage cells with the Block/Unblock tools first.", True)
            return
        if self.selected_slot is None:
            self._run(permissions=staged, label=f"permission cells in "
                                                f"{self.view_data['context']['map_member']}")
            return
        request = self._request()
        result = self.guard(lambda: self.project.apply_map_edit(
            self.project.doc["revision"], permissions=staged,
            label=f"move {self.view_data['context']['map_member']}:{self.selected_slot}", **request))
        if result is None:
            self.guard(self.refresh)
            return
        if not result["changed"]:
            self.notice("No byte would change; nothing was saved.")
        else:
            self._show_plan(result["transaction"], result["preview"], result["patch_count"], saved=True)
            self._clear_staged()
            self.notice(f"Saved transaction at revision {result['revision']}. Export a ROM for melonDS checks.")
        self.guard(self.refresh)

    def undo(self):
        if self.wf.actions or self.wf_active():
            return self.wf_guard(self.wf_undo)
        result = self.guard(lambda: self.project.undo(self.project.doc["revision"]))
        if result is not None:
            self.detail.setPlainText("")
            self._clear_staged()
            self.notice("Last edit undone and saved.")
        self.guard(self.refresh)

    # ---- export --------------------------------------------------------------

    def export_dialog(self):
        default = self.project.root / "exports" / f"map-authoring-r{self.project.doc['revision']}"
        target, _ = QFileDialog.getSaveFileName(self, "New ROM export folder", str(default))
        if target:
            self.export_to(target)

    def export_to(self, target, save=None):
        """Same export operation as the CLI: a new folder, never an overwrite."""
        if save is None:
            candidate = self.project.root / "playtest.sav"
            save = candidate if candidate.is_file() else None
        result = self.guard(lambda: self.project.export(
            target, self.project.doc["revision"], save))
        if result:
            self.notice(f"Exported {result['changed_byte_count']} changed byte(s) to "
                        f"{Path(target) / 'game.nds'}. Native melonDS acceptance is still pending; "
                        "run the checks in PLAYTEST.txt.")
        return result
