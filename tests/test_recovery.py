"""Portable packages and checkpoints (RECOVERY-01/02). Software checks only."""
import json
import os
import shutil
import zipfile
from pathlib import Path

import pytest

from sovereign_editor import recovery, core
from sovereign_editor.core import Project
from sovereign_editor.formats import EditorError, digest

ROOT = Path(__file__).resolve().parents[1]
PARENT = ROOT / 'projects/assets-gameplay-v1'
pytestmark = pytest.mark.skipif(not PARENT.is_dir(), reason='returned assets-gameplay-v1 (r64) parent absent')


def shop(name, items, before=None):
    return {'kind': 'data', 'context': {'header': 33, 'cell': [19, 12]},
            'request': {'operations': [{'kind': 'shop', 'name': name, 'before': before, 'items': items}], 'label': name}}


def leftovers(folder):
    return sorted(p.name for p in Path(folder).iterdir() if p.name.startswith('.'))


def test_package_relocates_and_exports_identically(tmp_path):
    a = Project(PARENT).clone(tmp_path / 'a')
    a.apply_area_edit(64, operations=[shop('pk_herbs', [17, 18])])
    out_a = a.export(tmp_path / 'export-a', 65)
    info = a.package(tmp_path / 'a.zip')
    assert info['files'] == 1 + sum(1 for p in (a.root / 'assets').rglob('*') if p.is_file()) and not info['baseline_included']
    doc_a = json.loads(a.path.read_text())
    moved = tmp_path / 'elsewhere'
    os.rename(a.root, moved)                                   # the original authoring path is gone
    b = recovery.unpack(tmp_path / 'a.zip', tmp_path / 'b', baseline=moved / 'baseline.nds')
    pb = Project(b['root'])
    assert json.loads(pb.path.read_text()) == doc_a and pb.doc['revision'] == 65
    out_b = pb.export(tmp_path / 'export-b', 65)
    assert out_a['candidate_sha256'] == out_b['candidate_sha256']
    shutil.rmtree(tmp_path / 'export-a'); shutil.rmtree(tmp_path / 'export-b')
    pb.apply_area_edit(65, operations=[shop('pk_herbs', [17, 18, 26], before=[17, 18])])   # revise after moving
    assert pb.doc['revision'] == 66
    # With the baseline inside, no ROM needs choosing.
    full = Project(moved).package(tmp_path / 'full.zip', include_baseline=True)
    assert full['baseline_included'] and full['bytes'] > 190_000_000
    c = recovery.unpack(tmp_path / 'full.zip', tmp_path / 'c')
    assert Project(c['root']).doc['revision'] == 65
    assert not leftovers(tmp_path)


def test_bad_packages_refuse_without_partial_projects(tmp_path):
    a = Project(PARENT).clone(tmp_path / 'a')
    a.package(tmp_path / 'a.zip')
    base = a.root / 'baseline.nds'

    def rewrite(name, change):
        target = tmp_path / name
        with zipfile.ZipFile(tmp_path / 'a.zip') as src, zipfile.ZipFile(target, 'w') as dst:
            for item in src.infolist():
                data = src.read(item.filename)
                result = change(item.filename, data)
                if result is not None:
                    dst.writestr(item.filename, result)
            extra = change('<extra>', None)
            if extra:
                dst.writestr(*extra)
        return target
    damaged = rewrite('damaged.zip', lambda n, d: (d.replace(b'"revision"', b'"revision" ') if n == 'project.json' else d)
                      if n != '<extra>' else None)
    extra = rewrite('extra.zip', lambda n, d: d if n != '<extra>' else ('notes.txt', b'hi'))
    unsafe = rewrite('unsafe.zip', lambda n, d: d if n != '<extra>' else ('../escape.txt', b'x'))
    missing = rewrite('missing.zip', lambda n, d: None if n.startswith('assets/') else (d if n != '<extra>' else None))
    other_rom = tmp_path / 'other.nds'
    blob = base.read_bytes()
    other_rom.write_bytes(blob[:-1] + bytes([blob[-1] ^ 0xFF]))       # same size, different ROM
    cases = [(damaged, base, 'INVALID_PACKAGE'), (extra, base, 'INVALID_PACKAGE'), (unsafe, base, 'INVALID_PACKAGE'),
             (missing, base, 'INVALID_PACKAGE'), (tmp_path / 'a.zip', other_rom, 'BASELINE_MISMATCH'),
             (tmp_path / 'a.zip', None, 'BASELINE_REQUIRED'), (tmp_path / 'a.zip', base, 'EXISTS')]
    for source, rom, code in cases:
        dest = tmp_path / ('a' if code == 'EXISTS' else 'dest')
        with pytest.raises(EditorError) as e:
            recovery.unpack(source, dest, baseline=rom)
        assert e.value.code == code, (source.name, str(e.value))
        assert code == 'EXISTS' or not dest.exists()
    assert not leftovers(tmp_path)


def test_checkpoint_restore_is_undoable_and_refuses_stale_clients(tmp_path):
    p = Project(PARENT).clone(tmp_path / 'p')
    start_doc = json.loads(p.path.read_text())
    assert p.checkpoint('start', 64, note='before shops')['revision'] == 64
    p.apply_area_edit(64, operations=[shop('cp_one', [17])])
    p.apply_area_edit(65, operations=[shop('cp_two', [18])])
    old_client = Project(p.root)                               # another window holding revision 66
    listing = {c['name']: c for c in p.checkpoints()['checkpoints']}
    assert listing['start']['compared_with_current']['only_in_current'] == 2
    result = p.restore_checkpoint('start', 66)
    assert result['revision'] == 67 and (p.root / 'checkpoints' / 'before-restore-r66').is_dir()
    now = json.loads(p.path.read_text())
    assert now['map_edits'] == start_doc['map_edits'] and now['revision'] == 67
    assert 'cp_one' not in p.composed().get('shops', {})
    with pytest.raises(EditorError) as e:
        old_client.apply_area_edit(66, operations=[shop('cp_three', [26])])
    assert e.value.code == 'STALE_REVISION'
    p.undo(67)                                                 # undo the restore itself
    assert set(p.composed().get('shops', {})) == {'cp_one', 'cp_two'}
    with pytest.raises(EditorError) as e:
        p.checkpoint('start', 68)
    assert e.value.code == 'EXISTS'
    # A damaged manifest (here: its revision field renamed) is refused before anything changes.
    manifest = p.root / 'checkpoints' / 'start' / 'checkpoint.json'
    manifest.write_text(manifest.read_text().replace('"revision"', '"revisioN"', 1))
    before = p.path.read_bytes()
    with pytest.raises(EditorError) as e:
        p.restore_checkpoint('start', 68)
    assert e.value.code == 'INVALID_CHECKPOINT' and p.path.read_bytes() == before
    assert {c['name']: c['valid'] for c in p.checkpoints()['checkpoints']}['start'] is False


def test_interrupted_writes_and_recovery_keep_the_last_valid_state(tmp_path, monkeypatch):
    p = Project(PARENT).clone(tmp_path / 'p')
    p.checkpoint('start', 64)
    before = p.path.read_bytes()
    real = os.replace

    def crash(src, dst):
        if str(dst).endswith('project.json'):
            raise OSError('simulated power loss during the write')
        return real(src, dst)
    monkeypatch.setattr(core.os, 'replace', crash)
    with pytest.raises(OSError):
        p.apply_area_edit(64, operations=[shop('crash', [17])])
    monkeypatch.setattr(core.os, 'replace', real)
    assert p.path.read_bytes() == before and leftovers(p.root) in ([], ['.project.lock'])
    assert Project(p.root).doc['revision'] == 64
    # An interrupted restore leaves the project unchanged and its automatic backup behind.
    p = Project(p.root)
    monkeypatch.setattr(Project, '_commit', lambda *a, **k: (_ for _ in ()).throw(OSError('crash in restore')))
    with pytest.raises(OSError):
        recovery.restore(p, 'start', 64)
    monkeypatch.undo()
    assert Project(p.root).path.read_bytes() == before and (p.root / 'checkpoints' / 'before-restore-r64').is_dir()
    # A project.json damaged outside the editor is rebuilt from a checkpoint; the damaged file is kept.
    p.path.write_text(p.path.read_text()[:500])
    with pytest.raises(Exception):
        Project(p.root)
    result = recovery.recover(p.root, 'start')
    assert result['revision'] == 64 and result['previous_file'].startswith('project.json.broken-')
    assert Project(p.root).doc['revision'] == 64


def test_checkpoints_dialog_and_open_package(tmp_path):
    import os as _os
    _os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    from sovereign_editor.recovery_ui import CheckpointsDialog, open_package
    from sovereign_editor.gui import STYLE
    app = QApplication.instance() or QApplication([]); app.setStyleSheet(STYLE)
    p = Project(PARENT).clone(tmp_path / 'p')
    changed = []
    d = CheckpointsDialog(p, on_changed=lambda: changed.append(True)); d.show(); app.processEvents()
    d.name.setText('ui-start'); d.note.setText('before the shop'); d.create(); app.processEvents()
    p.apply_area_edit(64, operations=[shop('ui_shop', [17])]); d.reload()
    d.list.setCurrentRow([d.list.item(i).data(0x0100) for i in range(d.list.count())].index('ui-start')); app.processEvents()
    assert 'Only in the current project: 1' in d.detail.toPlainText()
    out = ROOT / 'evidence/editor-completion-v1/ui'; out.mkdir(parents=True, exist_ok=True)
    assert d.grab().save(str(out / 'checkpoints.png'))
    d.restore(); app.processEvents()
    assert changed and p.doc['revision'] == 66 and 'ui_shop' not in p.composed().get('shops', {})
    d.package_dialog(str(tmp_path / 'ui.zip'))
    assert (tmp_path / 'ui.zip').is_file() and 'without the baseline' in d.status.text()
    result = open_package(None, tmp_path / 'ui.zip', tmp_path / 'opened', baseline=p.root / 'baseline.nds')
    assert Project(result['root']).doc['revision'] == 66
    d.close(); app.processEvents()
