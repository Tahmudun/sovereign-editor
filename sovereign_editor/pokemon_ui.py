"""Pokémon packages tab: import/revise prepared art packages and bind persistent form identities.

Backed only by Project.plan_area_edit / apply_area_edit with kind ``pokemon`` (the same
operation as the CLI area-edit command; pokemon-stage/pokemon-view for inspection).
"""
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QPushButton, QComboBox,
                               QSpinBox, QLineEdit, QPlainTextEdit, QGroupBox, QFileDialog)

from .formats import EditorError

CONTEXT = {'header': 67, 'cell': [17, 12]}


class PokemonPanel(QWidget):
    def __init__(self, project, on_applied=None):
        super().__init__()
        self.project, self.on_applied, self.operations = project, on_applied, []
        layout = QVBoxLayout(self)
        note = QLabel('A package is prepared battle front/back, palettes, party icon and follower art checked against '
                      'its template species. Binding it creates a permanent new form of that species (saves keep it); '
                      'stats, moves, cry, footprint and Pokédex entry are inherited until their own design is chosen.')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.list = QPlainTextEdit(); self.list.setReadOnly(True); self.list.setMaximumHeight(130)
        layout.addWidget(self.list)
        row = QHBoxLayout(); layout.addLayout(row)
        row.addWidget(self.import_group()); row.addWidget(self.bind_group())
        self.preview = QPlainTextEdit(); self.preview.setReadOnly(True); self.preview.setMaximumHeight(150)
        layout.addWidget(self.preview)
        buttons = QHBoxLayout(); layout.addLayout(buttons)
        for label, callback in (('Preview', self.preview_ops), ('Apply', self.apply), ('Discard', self.discard)):
            b = QPushButton(label); b.clicked.connect(lambda _=False, cb=callback: self.guard(cb)); buttons.addWidget(b)
        self.status = QLabel(); self.status.setWordWrap(True); layout.addWidget(self.status)
        layout.addStretch(1)
        self.reload()

    def guard(self, fn):
        try:
            fn()
        except (EditorError, ValueError, KeyError) as exc:
            self.status.setText(str(exc))

    def import_group(self):
        box = QGroupBox('Import or revise a package'); f = QFormLayout(box)
        self.source = QLineEdit(''); self.source.setPlaceholderText('folder with native/front.NCGR … follower.btx0')
        browse = QPushButton('Choose…')
        browse.clicked.connect(lambda: self.source.setText(
            QFileDialog.getExistingDirectory(self, 'Pokémon package folder') or self.source.text()))
        row = QHBoxLayout(); row.addWidget(self.source, 1); row.addWidget(browse)
        f.addRow('Folder', row)
        self.key = QLineEdit(''); self.key.setPlaceholderText('jacobs_eevee')
        self.display = QLineEdit('')
        self.template = QSpinBox(); self.template.setRange(1, 1075); self.template.setValue(133)
        self.palette = QComboBox()
        for i in range(3):
            self.palette.addItem(f'Icon palette {i}', i)
        for label, w in (('Package id', self.key), ('Display name', self.display), ('Template species', self.template),
                         ('Party icon', self.palette)):
            f.addRow(label, w)
        b = QPushButton('Stage import/revision'); b.clicked.connect(lambda: self.guard(self.stage_import)); f.addRow(b)
        return box

    def bind_group(self):
        box = QGroupBox('Bind a new form identity'); f = QFormLayout(box)
        self.identity = QLineEdit(''); self.identity.setPlaceholderText('jacobs_dog')
        self.package = QComboBox()
        self.evolution = QComboBox()
        self.evolution.addItem('Does not evolve', 'none'); self.evolution.addItem('Evolves like its species', 'inherit')
        for label, w in (('Identity key', self.identity), ('Package', self.package), ('Evolution', self.evolution)):
            f.addRow(label, w)
        b = QPushButton('Stage binding'); b.clicked.connect(lambda: self.guard(self.stage_bind)); f.addRow(b)
        return box

    def reload(self):
        self.revision = self.project.doc['revision']
        view = self.project.pokemon_view()
        lines = [f"package {p['key']} · {p['display']} · template {p['template']} · revision {p['revision']} · "
                 f"icon palette {p['icon_palette']} · identities {', '.join(p['identities']) or 'none'}"
                 for p in view['packages']]
        lines += [f"identity {i['key']} · species {i['species']} form {i['form']} · personal {i['personal']} · "
                  f"evolution {i['evolution']}" for i in view['identities']]
        lines.append(f"capacity: identities {view['capacity']['identities'][0]}/{view['capacity']['identities'][1]} · "
                     f"inherited: {view['inherited']}")
        self.list.setPlainText('\n'.join(lines))
        self.package.clear()
        for p in view['packages']:
            self.package.addItem(p['key'], p['key'])

    def stage_import(self):
        if not self.source.text().strip():
            raise ValueError('Choose a package folder.')
        request = {'key': self.key.text().strip(), 'source': self.source.text().strip(),
                   'display': self.display.text().strip(), 'template': self.template.value(),
                   'icon_palette': self.palette.currentData()}
        self.operations.append({'kind': 'pokemon', 'context': CONTEXT, 'request': request})
        self.status.setText('Staged · Preview validates the package against its template.')

    def stage_bind(self):
        request = {'action': 'bind', 'key': self.identity.text().strip(), 'package': self.package.currentData(),
                   'evolution': self.evolution.currentData()}
        self.operations.append({'kind': 'pokemon', 'context': CONTEXT, 'request': request})
        self.status.setText('Staged · Preview shows the allocated form and personal index.')

    def preview_ops(self):
        if not self.operations:
            raise ValueError('Stage an import or binding first.')
        plan = self.project.plan_area_edit(self.operations, 'Pokémon packages')
        lines = []
        for t in plan['transactions']:
            r = t['report'] or {}
            if t['action'] == 'bind':
                lines.append(f"bind {t['key']}: {t['after']['species']} form {r['form']} · personal {r['personal']} · "
                             f"evolution {r['evolution']} · inherits {', '.join(r['inherits'])}")
            else:
                lines.append(f"{t['action']} {t['key']}: roles {', '.join(r.get('roles', []))} · {r.get('sources')} "
                             f"source files · female art {r.get('female_art')} · identities {r.get('identities')}")
        self.preview.setPlainText('\n'.join(lines))
        self.status.setText('Preview validated · Apply saves one undoable edit.')

    def apply(self):
        if not self.operations:
            raise ValueError('Nothing staged.')
        result = self.project.apply_area_edit(self.revision, operations=self.operations, label='Pokémon packages')
        self.operations = []; self.reload()
        self.status.setText(f"Applied · revision {result['revision']}.")
        if self.on_applied:
            self.on_applied()

    def discard(self):
        self.operations = []; self.preview.clear(); self.status.setText('Staged Pokémon operations discarded.')
