"""Project browser (WORKSPACE-01..03, ACCESS-01): search, cross-links, progression, reach and
impact. Read-only views over Project.workspace_* / progression / reach / impact — the same
operations as the CLI. Navigation opens the map inspector at an area or an editor on a resource."""
import json
from pathlib import Path

from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QTabWidget, QWidget, QLineEdit, QComboBox, QListWidget,
                               QListWidgetItem, QLabel, QPushButton, QPlainTextEdit, QSplitter, QFileDialog, QSpinBox,
                               QCheckBox, QFormLayout)
from PySide6.QtCore import Qt

from .formats import EditorError

KINDS = ('area', 'character', 'trainer', 'state', 'event', 'shop', 'record', 'asset', 'connection', 'terrain')
LEVELS = ('error', 'conditional', 'warning', 'unanalyzed')


class ProjectBrowser(QDialog):
    def __init__(self, inspector):
        super().__init__(inspector)
        self.inspector, self.project = inspector, inspector.project
        self.setWindowTitle('Project browser · search, links, progression and impact')
        self.resize(1100, 760)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('Every authored resource with what it uses and who shares it; progression and reach '
                                'are static analysis (not native evidence) and never block export.'))
        self.tabs = QTabWidget(); layout.addWidget(self.tabs, 1)
        self.tabs.addTab(self.browse_tab(), 'Browse')
        self.tabs.addTab(self.progression_tab(), 'Progression')
        self.tabs.addTab(self.reach_tab(), 'Reach')
        self.tabs.addTab(self.impact_tab(), 'Impact')
        self.tabs.addTab(self.coverage_tab(), 'Coverage')
        self.status = QLabel(); self.status.setWordWrap(True); layout.addWidget(self.status)
        close = QPushButton('Close'); close.clicked.connect(self.accept)
        row = QHBoxLayout(); row.addStretch(1); row.addWidget(close); layout.addLayout(row)
        self.refresh()

    def guard(self, fn):
        try:
            return fn()
        except (EditorError, ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
            self.status.setText(f'{getattr(exc, "code", type(exc).__name__)}: {exc}')

    # ---- browse ----------------------------------------------------------------------------------

    def browse_tab(self):
        w = QWidget(); l = QVBoxLayout(w)
        row = QHBoxLayout(); l.addLayout(row)
        self.query = QLineEdit(); self.query.setPlaceholderText('Search names, keys and details (e.g. cut, robin, shop)')
        self.kind = QComboBox(); self.kind.addItem('All kinds', None)
        for k in KINDS:
            self.kind.addItem(k.title(), k)
        row.addWidget(self.query, 1); row.addWidget(self.kind)
        self.query.textChanged.connect(self.refresh); self.kind.currentIndexChanged.connect(self.refresh)
        split = QSplitter(); l.addWidget(split, 1)
        self.results = QListWidget(); self.results.currentItemChanged.connect(lambda *_: self.guard(self.show_entry))
        split.addWidget(self.results)
        right = QWidget(); r = QVBoxLayout(right); split.addWidget(right)
        self.detail = QLabel(); self.detail.setWordWrap(True); r.addWidget(self.detail)
        r.addWidget(QLabel('Uses')); self.uses = QListWidget(); r.addWidget(self.uses, 1)
        r.addWidget(QLabel('Used by (shared users)')); self.used_by = QListWidget(); r.addWidget(self.used_by, 1)
        for lst in (self.uses, self.used_by):
            lst.itemDoubleClicked.connect(lambda item: self.guard(lambda: self.jump(item.data(Qt.UserRole))))
        buttons = QHBoxLayout(); r.addLayout(buttons)
        self.open_area = QPushButton('Show area in map inspector'); self.open_area.clicked.connect(lambda: self.guard(self.goto_area))
        self.open_editor = QPushButton('Open in its editor'); self.open_editor.clicked.connect(lambda: self.guard(self.open_resource))
        buttons.addWidget(self.open_area); buttons.addWidget(self.open_editor)
        split.setSizes([420, 640])
        return w

    def refresh(self, *_):
        kinds = [self.kind.currentData()] if self.kind.currentData() else None
        data = self.guard(lambda: self.project.workspace_search(self.query.text(), kinds, limit=500))
        if not data:
            return
        self.results.blockSignals(True); self.results.clear()
        for e in data['entries']:
            item = QListWidgetItem(f"{e['kind']:<10} {e['name']}  ·  {len(e['used_by'])} user(s)")
            item.setData(Qt.UserRole, e['ref']); self.results.addItem(item)
        self.results.blockSignals(False)
        counts = ', '.join(f'{k} {v}' for k, v in sorted(data['counts'].items()))
        self.status.setText(f"{data['total']} match(es) · project revision {data['revision']} · {counts}")
        if self.results.count():
            self.results.setCurrentRow(0)

    def current_ref(self):
        item = self.results.currentItem()
        return item.data(Qt.UserRole) if item else None

    def show_entry(self):
        ref = self.current_ref()
        if not ref:
            return
        e = self.project.workspace_references(ref)
        where = f" · header {e['header']}" + (f" cell {e['cell']}" if e.get('cell') else '') if e.get('header') is not None else ''
        self.detail.setText(f"<b>{e['name']}</b> ({e['kind']}){where}<br>{e['detail']}"
                            + ('<br><i>Shared: every user below sees a change to it.</i>' if e['shared'] else ''))
        for lst, rows in ((self.uses, e['uses']), (self.used_by, e['used_by'])):
            lst.clear()
            for u in rows:
                item = QListWidgetItem(f"{u['kind']}: {u['name']} — {u['detail']}"); item.setData(Qt.UserRole, u['ref']); lst.addItem(item)
        self.open_area.setEnabled(e.get('header') is not None)
        self.open_editor.setEnabled(e['kind'] in ('event', 'trainer', 'state', 'character', 'shop', 'record', 'area', 'terrain', 'connection'))

    def jump(self, ref):
        """Follow a cross-link inside the browser."""
        self.query.setText(''); self.kind.setCurrentIndex(0)
        for i in range(self.results.count()):
            if self.results.item(i).data(Qt.UserRole) == ref:
                self.results.setCurrentRow(i)
                return
        raise ValueError(f'{ref} is not in the list')

    def goto_area(self):
        e = self.project.workspace_references(self.current_ref())
        cell = e.get('cell') or [0, 0]
        if e['header'] is None:
            raise ValueError('This resource has no area')
        if hasattr(self.inspector, 'load_context'):
            try:
                self.inspector.load_context(e['header'], list(cell))
            except EditorError:
                ctx = self.project.context(header=e['header'])
                self.inspector.load_context(e['header'], [ctx['cell']['x'], ctx['cell']['y']])
            self.status.setText(f"Map inspector now shows header {e['header']}.")

    def open_resource(self):
        e = self.project.workspace_references(self.current_ref())
        if e['kind'] in ('event', 'trainer', 'state', 'character'):
            if e.get('header') is not None:
                self.goto_area()
            from .story_ui import StoryEditor
            editor = StoryEditor(self.inspector)
            tab = {'character': 0, 'trainer': 1, 'state': 2, 'event': 3}[e['kind']]
            editor.tabs.setCurrentIndex(tab)
            combo = {'trainer': getattr(editor, 'trainers', None), 'event': getattr(editor, 'events', None)}.get(e['kind'])
            if combo is not None and combo.findData(e['key']) >= 0:
                combo.setCurrentIndex(combo.findData(e['key']))
            self.opened = editor
            editor.exec()
        elif e['kind'] in ('shop', 'record'):
            from .gameplay_ui import GameplayEditor
            editor = GameplayEditor(self.project, self, header=getattr(self.inspector, 'header', 67))
            editor.tabs.setCurrentWidget(editor.records)
            self.opened = editor
            editor.exec()
        elif e['kind'] in ('area', 'terrain', 'connection'):
            if e.get('header') is not None:
                self.goto_area()
            from .world_ui import WorldEditor
            editor = WorldEditor(self.inspector)
            self.opened = editor
            editor.exec()
        self.refresh()

    # ---- progression -----------------------------------------------------------------------------

    def progression_tab(self):
        w = QWidget(); l = QVBoxLayout(w)
        row = QHBoxLayout(); l.addLayout(row)
        self.level = QComboBox(); self.level.addItem('All findings', None)
        for lv in LEVELS:
            self.level.addItem(lv.title(), lv)
        run = QPushButton('Analyse progression'); run.clicked.connect(lambda: self.guard(self.run_progression))
        self.level.currentIndexChanged.connect(lambda *_: self.guard(self.show_findings))
        row.addWidget(self.level); row.addWidget(run); row.addStretch(1)
        self.findings = QListWidget(); l.addWidget(self.findings, 2)
        l.addWidget(QLabel('Links (warps and authored travel)')); self.edges = QListWidget(); l.addWidget(self.edges, 1)
        self.progress_data = None
        return w

    def run_progression(self):
        self.progress_data = self.project.progression()
        self.edges.clear()
        for e in self.progress_data['edges']:
            cond = f"  needs {'; '.join(e['conditions'])}" if e['conditions'] else ''
            self.edges.addItem(f"{e['kind']}: header {e['from']} {e['tile']} → header {e['to']}{cond}")
        self.show_findings()
        s = self.progress_data['summary']
        self.status.setText('Progression: ' + (', '.join(f'{k} {v}' for k, v in sorted(s.items())) or 'no findings')
                            + ' · ' + self.progress_data['notes'][0])

    def show_findings(self):
        if not self.progress_data:
            return
        want = self.level.currentData()
        self.findings.clear()
        for f in self.progress_data['findings']:
            if want and f['level'] != want:
                continue
            self.findings.addItem(f"[{f['level']}] header {f['area']}: {f['message']}")

    # ---- reach -----------------------------------------------------------------------------------

    def reach_tab(self):
        w = QWidget(); f = QFormLayout(w)
        self.save_path = QLineEdit(); self.save_path.setPlaceholderText('game.sav (read-only) — or use the tile below')
        pick = QPushButton('Choose save…'); pick.clicked.connect(self.choose_save)
        row = QHBoxLayout(); row.addWidget(self.save_path, 1); row.addWidget(pick); f.addRow('Played save', row)
        self.reach_header, self.reach_x, self.reach_z = QSpinBox(), QSpinBox(), QSpinBox()
        for s in (self.reach_header, self.reach_x, self.reach_z):
            s.setRange(0, 65535)
        self.reach_header.setValue(getattr(self.inspector, 'header', 67) or 67)
        tile = QHBoxLayout()
        for label, s in (('header', self.reach_header), ('x', self.reach_x), ('z', self.reach_z)):
            tile.addWidget(QLabel(label)); tile.addWidget(s)
        f.addRow('Or start tile', tile)
        self.reach_surf = QCheckBox('Allow Surf water'); f.addRow('', self.reach_surf)
        run = QPushButton('Analyse reach'); run.clicked.connect(lambda: self.guard(self.run_reach)); f.addRow('', run)
        self.reach_out = QPlainTextEdit(); self.reach_out.setReadOnly(True); f.addRow(self.reach_out)
        return w

    def choose_save(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Choose a save (read only)', str(Path.home()), 'Saves (*.sav);;All files (*)')
        if path:
            self.save_path.setText(path)

    def run_reach(self):
        save = self.save_path.text().strip() or None
        r = (self.project.reach(save=save, surf=self.reach_surf.isChecked()) if save else
             self.project.reach(self.reach_header.value(), self.reach_x.value(), self.reach_z.value(), surf=self.reach_surf.isChecked()))
        lines = []
        if r.get('save'):
            s = r['save']
            lines.append(f"Save slot {s['slot']} (count {s['save_count']}): header {s['header']} tile {s['x']},{s['z']} facing {s['facing']}")
        lines.append(f"Reachable tiles {r['tiles']} · by header {r['by_header']}")
        lines.append(f"Authored events reached {r['reached']} of {len(r['events'])}")
        for link in r['links']:
            cond = f" needs {'; '.join(link.get('conditions') or [])}" if link.get('conditions') else ''
            lines.append(f"  {link['kind']}: {link['from']} → header {link['to']} at {link['arrival']}{cond}")
        missing = [e for e in r['events'] if not e['reachable']]
        if missing:
            lines.append('Not reached: ' + ', '.join(f"{e['event']} (h{e['header']} {e['tile']})" for e in missing[:40]))
        lines.append(r['rules'])
        self.reach_out.setPlainText('\n'.join(lines))
        self.status.setText('Reach analysed (static; native play is the acceptance).')

    # ---- impact ----------------------------------------------------------------------------------

    def impact_tab(self):
        w = QWidget(); l = QVBoxLayout(w)
        l.addWidget(QLabel('Paste or load a batch request ({"operations": [...], "label": ...}); nothing is written.'))
        self.request = QPlainTextEdit(); self.request.setPlaceholderText('{"operations": [...]}'); l.addWidget(self.request, 1)
        row = QHBoxLayout(); l.addLayout(row)
        load = QPushButton('Load request…'); load.clicked.connect(self.load_request)
        run = QPushButton('Report impact'); run.clicked.connect(lambda: self.guard(self.run_impact))
        row.addWidget(load); row.addWidget(run); row.addStretch(1)
        self.impact_out = QPlainTextEdit(); self.impact_out.setReadOnly(True); l.addWidget(self.impact_out, 1)
        return w

    def load_request(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Batch request', str(Path.home()), 'JSON (*.json)')
        if path:
            self.request.setPlainText(Path(path).read_text())

    def run_impact(self):
        request = json.loads(self.request.toPlainText())
        r = self.project.impact(**request)
        lines = [f"{r['transactions']} transaction(s) · {r['label']} · {r['undo']}"]
        for a in r['affected']:
            shared = f" · shared by {', '.join(a['shared_by'][:8])}" if a['shared_by'] else ''
            lines.append(f"  {a['change']:<8} {a['kind']}: {a['name']}{shared}")
        self.impact_out.setPlainText('\n'.join(lines))
        self.status.setText('Impact reported; nothing was written.')

    # ---- coverage ---------------------------------------------------------------------------------

    def coverage_tab(self):
        from PySide6.QtWidgets import QTableWidget, QTableWidgetItem, QHeaderView
        w = QWidget(); l = QVBoxLayout(w)
        report = self.guard(self.project.coverage)
        if not report:
            return w
        l.addWidget(QLabel(report['note'] + '  ' + ', '.join(f'{k}: {v}' for k, v in sorted(report['counts'].items()))))
        self.coverage_table = QTableWidget(len(report['rows']), 4)
        self.coverage_table.setHorizontalHeaderLabels(['Area', 'Feature', 'Status', 'Limits'])
        for i, r in enumerate(report['rows']):
            for j, key in enumerate(('area', 'feature', 'status', 'limits')):
                item = QTableWidgetItem(r[key]); item.setToolTip(r[key] + (f"\nEvidence: {r['evidence']}" if r['evidence'] else ''))
                self.coverage_table.setItem(i, j, item)
        self.coverage_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.coverage_table.setWordWrap(True)
        l.addWidget(self.coverage_table, 3)
        limits = QListWidget()
        for k, v in sorted(report['limits'].items()):
            limits.addItem(f"{k}: {v['used']}/{v['limit']}" if isinstance(v, dict) and 'used' in v else f'{k}: {v}')
        l.addWidget(QLabel('Live limits of this project')); l.addWidget(limits, 1)
        return w
