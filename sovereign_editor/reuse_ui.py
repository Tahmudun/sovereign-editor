"""Reuse content from another project (PROD-01): pick definitions, preview dependencies, apply.

Backed only by reuse.plan / reuse.apply (the CLI ``reuse`` command runs the same functions).
"""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit, QListWidget,
                               QListWidgetItem, QPlainTextEdit, QCheckBox, QFileDialog, QAbstractItemView)

from . import reuse
from .formats import EditorError

FIXTURE_PREFIXES = ('load_', 'practice_')


class ReuseDialog(QDialog):
    def __init__(self, project, parent=None):
        super().__init__(parent)
        self.project, self.source = project, None
        self.setWindowTitle('Sovereign Editor · Reuse content')
        self.resize(760, 640)
        layout = QVBoxLayout(self)
        note = QLabel('Choose characters, trainers, states, prop assets, ground materials, Pokémon packages and environments from '
                      'another project. Dependencies come along; this project allocates its own IDs. Created areas, '
                      'scenes, events and history stay in the source.')
        note.setWordWrap(True); layout.addWidget(note)
        row = QHBoxLayout(); layout.addLayout(row)
        self.path = QLineEdit(''); self.path.setPlaceholderText('source project folder')
        browse = QPushButton('Choose…'); browse.clicked.connect(self.choose)
        load = QPushButton('Load'); load.clicked.connect(lambda: self.guard(self.load))
        for w in (self.path, browse, load):
            row.addWidget(w)
        self.fixtures = QCheckBox('Show load-test and practice fixtures'); self.fixtures.toggled.connect(
            lambda *_: self.guard(self.fill))
        layout.addWidget(self.fixtures)
        self.items = QListWidget(); self.items.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        layout.addWidget(self.items, 1)
        self.preview = QPlainTextEdit(); self.preview.setReadOnly(True); self.preview.setMaximumHeight(170)
        layout.addWidget(self.preview)
        buttons = QHBoxLayout(); layout.addLayout(buttons)
        for label, callback in (('Preview', self.preview_plan), ('Apply', self.apply)):
            b = QPushButton(label); b.clicked.connect(lambda _=False, cb=callback: self.guard(cb)); buttons.addWidget(b)
        close = QPushButton('Close'); close.clicked.connect(self.reject); buttons.addWidget(close)
        self.status = QLabel(); self.status.setWordWrap(True); layout.addWidget(self.status)

    def guard(self, fn):
        try:
            fn()
        except (EditorError, ValueError, KeyError, OSError) as exc:
            self.status.setText(str(exc))

    def choose(self):
        folder = QFileDialog.getExistingDirectory(self, 'Source project')
        if folder:
            self.path.setText(folder)
            self.guard(self.load)

    def load(self):
        from .core import Project
        self.source = Project(Path(self.path.text().strip()))
        self.fill()
        self.status.setText(f"Loaded {self.source.doc.get('name', '')} · revision {self.source.doc['revision']}.")

    def fill(self):
        self.items.clear()
        if self.source is None:
            return
        for kind, table in reuse._catalogs(self.source.composed()).items():
            for key, value in sorted(table.items()):
                if value.get('retired'):
                    continue
                if not self.fixtures.isChecked() and key.startswith(FIXTURE_PREFIXES):
                    continue
                name = value.get('name') or value.get('display') or ''
                item = QListWidgetItem(f'{kind}: {key}' + (f' · {name}' if name else ''))
                item.setData(Qt.ItemDataRole.UserRole, f'{kind}:{key}')
                self.items.addItem(item)

    def selection(self):
        chosen = [i.data(Qt.ItemDataRole.UserRole) for i in self.items.selectedItems()]
        if not chosen:
            raise ValueError('Select at least one item.')
        return chosen

    def preview_plan(self):
        result = reuse.plan(self.project, self.source, self.selection())
        self.preview.setPlainText('\n'.join(f"{r['kind']} {r['key']} · {r['reason']} · {r['result']}"
                                            for r in result['items']) + f"\n{result['not_reused']}")
        self.status.setText('Preview only · Apply adds these as one undoable edit.')

    def apply(self):
        result = reuse.apply(self.project, self.source, self.selection(), self.project.doc['revision'])
        self.preview_plan()
        self.status.setText(f"Applied · revision {result['revision']}.")
