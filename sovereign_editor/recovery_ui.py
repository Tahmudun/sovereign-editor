"""Checkpoints and portable packages (RECOVERY-01/02) — the same operations as the CLI."""
from pathlib import Path

from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem, QLabel, QPushButton,
                               QLineEdit, QPlainTextEdit, QFileDialog, QCheckBox, QFormLayout)
from PySide6.QtCore import Qt

from .formats import EditorError


class CheckpointsDialog(QDialog):
    def __init__(self, project, parent=None, on_changed=None):
        super().__init__(parent)
        self.project, self.on_changed = project, on_changed
        self.setWindowTitle('Checkpoints and packages')
        self.resize(820, 620)
        l = QVBoxLayout(self)
        note = QLabel('A checkpoint keeps project.json and its asset packages. Restoring is one undoable edit: an '
                      'automatic "before-restore" checkpoint is made first, the revision advances (other open windows '
                      'must reload) and Undo returns to the state before the restore. The baseline ROM is never changed.')
        note.setWordWrap(True); l.addWidget(note)
        self.list = QListWidget(); self.list.currentItemChanged.connect(lambda *_: self.show_selected()); l.addWidget(self.list, 1)
        self.detail = QPlainTextEdit(); self.detail.setReadOnly(True); self.detail.setMaximumHeight(140); l.addWidget(self.detail)
        form = QFormLayout(); l.addLayout(form)
        self.name = QLineEdit(); self.name.setPlaceholderText('e.g. before-chapter-revision')
        self.note = QLineEdit(); self.note.setPlaceholderText('optional note')
        form.addRow('New checkpoint', self.name); form.addRow('Note', self.note)
        row = QHBoxLayout(); l.addLayout(row)
        for label, fn in (('Create checkpoint', self.create), ('Restore selected', self.restore),
                          ('Package project…', self.package_dialog)):
            b = QPushButton(label); b.clicked.connect(lambda _=False, fn=fn: self.guard(fn)); row.addWidget(b)
        self.with_baseline = QCheckBox('Include the baseline ROM in the package'); row.addWidget(self.with_baseline)
        self.status = QLabel(); self.status.setWordWrap(True); l.addWidget(self.status)
        close = QPushButton('Close'); close.clicked.connect(self.accept); l.addWidget(close, 0, Qt.AlignRight)
        self.reload()

    def guard(self, fn):
        try:
            fn()
        except (EditorError, OSError, ValueError) as exc:
            self.status.setText(f'{getattr(exc, "code", type(exc).__name__)}: {exc}')

    def reload(self):
        data = self.project.checkpoints()
        self.rows = {c['name']: c for c in data['checkpoints']}
        self.list.clear()
        for c in data['checkpoints']:
            text = (f"{c['name']} · revision {c['revision']} · {c['created']}" + (f" · {c['note']}" if c.get('note') else '')
                    if c['valid'] else f"{c['name']} · damaged: {c['reason']}")
            item = QListWidgetItem(text); item.setData(Qt.UserRole, c['name']); self.list.addItem(item)
        self.status.setText(f"Project revision {data['revision']} · {len(self.rows)} checkpoint(s)")

    def selected(self):
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def show_selected(self):
        c = self.rows.get(self.selected())
        if not c:
            self.detail.clear(); return
        if not c['valid']:
            self.detail.setPlainText(c['reason']); return
        diff = c['compared_with_current']
        self.detail.setPlainText(
            f"Transactions shared with the current project: {diff['shared_transactions']}\n"
            f"Only in the checkpoint: {diff['only_in_checkpoint']}   Only in the current project: {diff['only_in_current']}\n"
            + ('Checkpoint-only edits: ' + ', '.join(str(x) for x in diff['labels_only_in_checkpoint']) if diff['labels_only_in_checkpoint'] else ''))

    def create(self):
        result = self.project.checkpoint(self.name.text().strip(), self.project.doc['revision'], self.note.text().strip() or None)
        self.name.clear(); self.note.clear(); self.reload()
        self.status.setText(f"Checkpoint {result['name']} saved at revision {result['revision']}.")

    def restore(self):
        name = self.selected()
        if not name:
            raise ValueError('Select a checkpoint to restore')
        result = self.project.restore_checkpoint(name, self.project.doc['revision'])
        self.reload()
        self.status.setText(f"Restored {name} as revision {result['revision']}; automatic backup "
                            f"{result['automatic_backup']}. {result['undo']}.")
        if self.on_changed:
            self.on_changed()

    def package_dialog(self, path=None):
        if path is None:
            path, _ = QFileDialog.getSaveFileName(self, 'Package project', str(Path.home() / f"{self.project.root.name}.zip"),
                                                  'Project package (*.zip)')
        if path:
            result = self.project.package(path, self.with_baseline.isChecked())
            self.status.setText(f"Package written: {result['package']} ({result['files']} files, "
                                f"{'with' if result['baseline_included'] else 'without'} the baseline ROM).")


def open_package(parent, package=None, destination=None, baseline=None):
    """Unpack flow: package → destination folder → (baseline ROM when the package lacks it)."""
    from . import recovery
    if package is None:
        package, _ = QFileDialog.getOpenFileName(parent, 'Open project package', str(Path.home()), 'Project package (*.zip)')
        if not package:
            return None
    if destination is None:
        destination = QFileDialog.getExistingDirectory(parent, 'Folder to create the project in', str(Path.home()))
        if not destination:
            return None
        destination = Path(destination) / Path(package).stem
    try:
        return recovery.unpack(package, destination, baseline)
    except EditorError as exc:
        if exc.code != 'BASELINE_REQUIRED' or baseline is not None:
            raise
        rom, _ = QFileDialog.getOpenFileName(parent, str(exc), str(Path.home()), 'Nintendo DS ROM (*.nds)')
        if not rom:
            return None
        return recovery.unpack(package, destination, rom)
